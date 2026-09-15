"""Tenant self-service endpoints — view and update the caller's own tenant."""
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.schemas.tenant import TenantOut, TenantUpdate
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
