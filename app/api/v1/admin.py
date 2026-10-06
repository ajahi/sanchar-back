"""Platform super-admin — cross-tenant overview and the per-tenant AI auto-reply switch."""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentUser, require_role
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.audit import record_audit

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("super_admin"))])


class AiToggle(BaseModel):
    enabled: bool


async def _per_tenant(db: AsyncSession, stmt) -> dict:
    return {tid: n for tid, n in (await db.execute(stmt)).all()}


@router.get("/tenants")
async def list_tenants(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    """One row per tenant with its activity counts. ponytail: unpaginated, 5 grouped queries; page it past ~1k tenants."""
    tenants = (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()

    convs = await _per_tenant(db, select(Conversation.tenant_id, func.count()).group_by(Conversation.tenant_id))
    handover = await _per_tenant(
        db,
        select(Conversation.tenant_id, func.count())
        .where(Conversation.status == "open", Conversation.mode != "ai")
        .group_by(Conversation.tenant_id),
    )
    custs = await _per_tenant(db, select(Customer.tenant_id, func.count()).group_by(Customer.tenant_id))
    msgs = (
        await db.execute(
            select(
                Conversation.tenant_id,
                func.count(),
                func.count().filter(Message.sender_type == "ai"),
                func.max(Message.created_at),
            )
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .group_by(Conversation.tenant_id)
        )
    ).all()
    msgs = {r[0]: r[1:] for r in msgs}
    accounts = {}
    for tid, platform in (await db.execute(select(SocialAccount.tenant_id, SocialAccount.platform).where(SocialAccount.status == "active"))).all():
        accounts.setdefault(tid, []).append(platform)
    knowledge = set(
        (await db.execute(select(distinct(KnowledgeDocument.tenant_id)).where(func.length(func.trim(KnowledgeDocument.content)) > 0))).scalars()
    )

    return [
        {
            "id": t.id,
            "name": t.name,
            "owner_name": t.owner_name,
            "status": t.status,
            "ai_auto_reply": t.ai_auto_reply,
            "has_knowledge": t.id in knowledge,
            "channels": sorted(set(accounts.get(t.id, []))),
            "customers": custs.get(t.id, 0),
            "conversations": convs.get(t.id, 0),
            "open_handovers": handover.get(t.id, 0),
            "messages": msgs.get(t.id, (0, 0, None))[0],
            "ai_replies": msgs.get(t.id, (0, 0, None))[1],
            "last_message_at": msgs.get(t.id, (0, 0, None))[2],
            "created_at": t.created_at,
        }
        for t in tenants
    ]


@router.patch("/tenants/{tenant_id}/ai")
async def set_tenant_ai(
    tenant_id: uuid.UUID,
    body: AiToggle,
    admin: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Switch a tenant's AI auto-reply on/off. Platform-only: tenants can no longer change it."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenant.ai_auto_reply = body.enabled
    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=admin.id,
        actor_type="super_admin",
        action="ai_auto_reply_on" if body.enabled else "ai_auto_reply_off",
        entity_type="tenant",
        entity_id=tenant.id,
    )
    await db.commit()
    return {"id": tenant.id, "ai_auto_reply": tenant.ai_auto_reply}
