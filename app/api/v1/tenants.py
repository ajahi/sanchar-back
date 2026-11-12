"""Tenant self-service endpoints — view and update the caller's own tenant, and its "we are closed" switch."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.tenant import Tenant
from app.schemas.tenant import ClosureIn, ClosureOut, TenantOut, TenantUpdate
from app.services import hours
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


def _closure_out(tenant: Tenant) -> ClosureOut:
    c = tenant.closure or {}
    message = hours.closure_message(tenant.closure)
    return ClosureOut(
        closed=message is not None, message=message, custom_message=c.get("message"),
        until=c.get("until") if message else None, set_at=c.get("set_at") if message else None,
    )


@router.get("/me/closure", response_model=ClosureOut)
async def get_closure(tenant: CurrentTenant) -> ClosureOut:
    """Is the shop switched to closed, and what does the bot tell customers who want to order."""
    return _closure_out(tenant)


@router.put("/me/closure", response_model=ClosureOut, dependencies=[Depends(require_role("owner", "admin"))])
async def close_shop(
    body: ClosureIn, tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> ClosureOut:
    """Switch the shop to closed (or change the message / end while closed). The bot takes no orders and answers
    an order with the message; questions are still answered."""
    now = datetime.now(hours.NEPAL)
    until = hours.end_of_today(now) if body.for_today else body.until
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=hours.NEPAL)
    if until is not None and until <= now:
        raise HTTPException(status_code=422, detail="The closing end time must be in the future.")
    tenant.closure = {
        "message": (body.message or "").strip() or None,
        "until": until.isoformat() if until else None,
        "set_at": now.isoformat(),
        "set_by": str(user.id),
    }
    await record_audit(
        db, tenant_id=tenant.id, actor_id=user.id, action="shop_closed", entity_type="tenant", entity_id=tenant.id,
        meta={"until": tenant.closure["until"], "message": tenant.closure["message"]},
    )
    await db.commit()
    return _closure_out(tenant)


@router.delete("/me/closure", response_model=ClosureOut, dependencies=[Depends(require_role("owner", "admin"))])
async def reopen_shop(
    tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> ClosureOut:
    """Back open now. The owner's message is kept (ended now) so the screen can offer it next time."""
    if tenant.closure:
        now = datetime.now(hours.NEPAL).isoformat()
        tenant.closure = {**tenant.closure, "until": now, "reopened_at": now}
        await record_audit(
            db, tenant_id=tenant.id, actor_id=user.id, action="shop_reopened", entity_type="tenant", entity_id=tenant.id,
        )
        await db.commit()
    return _closure_out(tenant)
