"""Tenant self-service endpoints — view and update the caller's own tenant, and its "we are closed" switch."""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.tenant import Tenant
from app.schemas.tenant import CloseConditionsIn, CloseConditionsOut, TenantOut, TenantUpdate
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
