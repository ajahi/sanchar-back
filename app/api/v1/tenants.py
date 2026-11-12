"""Tenant self-service endpoints — view and update the caller's own tenant, and its "we are closed" switch."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.tenant import Tenant
from app.schemas.tenant import CloseConditionsIn, CloseConditionsOut, JsonPostIn, JsonPostOut, TenantOut, TenantUpdate
from app.services import hours, json_post
from app.services.audit import record_audit

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantOut)
async def get_my_tenant(tenant: CurrentTenant) -> TenantOut:
    return TenantOut.model_validate(tenant)


@router.patch(
    "/me",
    response_model=TenantOut,
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def update_my_tenant(
    body: TenantUpdate,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TenantOut:
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(tenant, field, value)

    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=user.id,
        action="tenant_updated",
        entity_type="tenant",
        entity_id=tenant.id,
        meta={"fields": list(changes.keys())},
    )
    await db.commit()
    await db.refresh(tenant)
    return TenantOut.model_validate(tenant)


def _close_conditions_out(tenant: Tenant) -> CloseConditionsOut:
    c = tenant.close_conditions or {}
    message = hours.close_conditions_message(tenant.close_conditions)
    return CloseConditionsOut(
        closed=message is not None, message=message, custom_message=c.get("message"),
        until=c.get("until") if message else None, set_at=c.get("set_at") if message else None,
    )


@router.get("/me/close-conditions", response_model=CloseConditionsOut)
async def get_close_conditions(tenant: CurrentTenant) -> CloseConditionsOut:
    """Is the shop switched to closed, and what does the bot tell customers who want to order."""
    return _close_conditions_out(tenant)


@router.put("/me/close-conditions", response_model=CloseConditionsOut, dependencies=[Depends(require_role("owner", "admin"))])
async def close_shop(
    body: CloseConditionsIn, tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> CloseConditionsOut:
    """Switch the shop to closed (or change the message / end while closed). The bot takes no orders and answers
    an order with the message; questions are still answered."""
    now = datetime.now(hours.NEPAL)
    until = hours.end_of_today(now) if body.for_today else body.until
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=hours.NEPAL)
    if until is not None and until <= now:
        raise HTTPException(status_code=422, detail="The closing end time must be in the future.")
    tenant.close_conditions = {
        "message": (body.message or "").strip() or None,
        "until": until.isoformat() if until else None,
        "set_at": now.isoformat(),
        "set_by": str(user.id),
    }
    await record_audit(
        db, tenant_id=tenant.id, actor_id=user.id, action="shop_closed", entity_type="tenant", entity_id=tenant.id,
        meta={"until": tenant.close_conditions["until"], "message": tenant.close_conditions["message"]},
    )
    await db.commit()
    return _close_conditions_out(tenant)


@router.delete("/me/close-conditions", response_model=CloseConditionsOut, dependencies=[Depends(require_role("owner", "admin"))])
async def reopen_shop(
    tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> CloseConditionsOut:
    """Back open now. The owner's message is kept (ended now) so the screen can offer it next time."""
    if tenant.close_conditions:
        now = datetime.now(hours.NEPAL).isoformat()
        tenant.close_conditions = {**tenant.close_conditions, "until": now, "reopened_at": now}
        await record_audit(
            db, tenant_id=tenant.id, actor_id=user.id, action="shop_reopened", entity_type="tenant", entity_id=tenant.id,
        )
        await db.commit()
    return _close_conditions_out(tenant)


# ---- Confirmed orders also sent as JSON to the business's own API (services/json_post.py) ----

_owner_only = [Depends(require_role("owner", "admin"))]


def _json_post_out(tenant: Tenant) -> JsonPostOut:
    c = tenant.json_post or {}
    return JsonPostOut(url=c.get("url"), enabled=bool(c.get("enabled")), secret=c.get("secret"))


@router.get("/me/json-post", response_model=JsonPostOut, dependencies=_owner_only)
async def get_json_post(tenant: CurrentTenant) -> JsonPostOut:
    return _json_post_out(tenant)


@router.put("/me/json-post", response_model=JsonPostOut, dependencies=_owner_only)
async def save_json_post(
    body: JsonPostIn, tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> JsonPostOut:
    """Set the address confirmed orders are also POSTed to as JSON, and switch it on or off. The website checkout
    post (Order system) is not touched. A secret for the signature is made the first time."""
    url = (body.url or "").strip() or None
    if body.enabled and not url:
        raise HTTPException(status_code=422, detail="Add your API address before switching it on.")
    if url and (problem := await json_post.url_problem(url)):
        raise HTTPException(status_code=422, detail=problem)
    old = tenant.json_post or {}
    tenant.json_post = {"url": url, "enabled": body.enabled, "secret": old.get("secret") or json_post.new_secret()}
    await record_audit(db, tenant_id=tenant.id, actor_id=user.id, action="json_post_saved", entity_type="tenant",
                       entity_id=tenant.id, meta={"url": url, "enabled": body.enabled})
    await db.commit()
    return _json_post_out(tenant)


@router.post("/me/json-post/new-secret", response_model=JsonPostOut, dependencies=_owner_only)
async def rotate_json_post_secret(
    tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> JsonPostOut:
    """A new signing secret (the old one stops working at once: update your API too)."""
    tenant.json_post = {**(tenant.json_post or {}), "secret": json_post.new_secret()}
    await record_audit(db, tenant_id=tenant.id, actor_id=user.id, action="json_post_secret_rotated",
                       entity_type="tenant", entity_id=tenant.id)
    await db.commit()
    return _json_post_out(tenant)


@router.post("/me/json-post/test", dependencies=_owner_only)
async def test_json_post(tenant: CurrentTenant) -> dict:
    """Send a sample order (event "order.test") to the saved address, signed like a real one: the API's answer."""
    c = tenant.json_post or {}
    if not c.get("url"):
        raise HTTPException(status_code=422, detail="Save your API address first.")
    sample = {
        "event": "order.test", "id": "test", "created_at": datetime.now(hours.NEPAL).isoformat(),
        "business": {"id": str(tenant.id), "name": tenant.name}, "funnel": {"key": "restaurant", "version": 1},
        "customer": {"name": "Test Customer", "phone": "9800000000", "channel": "instagram", "handle": "test"},
        "fields": {"name": "Test Customer", "phone": "9800000000", "address": "Pick up", "items": "1 x Test Item"},
        "order": {"items": [{"menu_id": "test", "name": "Test Item", "quantity": 1, "price": 100, "amount": 100}],
                  "subtotal": 100, "delivery": {"type": "pickup", "area": None, "charge": 0, "listed": True},
                  "total": 100, "unmatched": []},
        "conversation_id": None,
    }
    return await json_post.send(c["url"], c.get("secret") or "", "order.test", "test", sample)
