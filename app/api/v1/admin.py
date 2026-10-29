"""Platform super-admin — cross-tenant overview and the per-tenant AI auto-reply switch."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentUser, require_role
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.funnel import Funnel, FunnelSubmission
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import funnels, hours
from app.services.audit import record_audit
from app.services.reply_pipeline import preview_reply

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("super_admin"))])
NEPAL = timezone(timedelta(hours=5, minutes=45))  # no daylight saving


class AiToggle(BaseModel):
    enabled: bool


class FunnelField(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,29}$")  # where the value is saved
    label: str = Field(min_length=1, max_length=80)  # what the model writes in its place in the label
    required: bool = True
    max: int = Field(default=300, ge=10, le=2000)


class FunnelIn(BaseModel):
    """A funnel version. steps must hold {label} (where the model is told the label to write)."""
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,49}$")
    name: str = Field(min_length=2, max_length=100)
    label: str = Field(pattern=r"^[a-z][a-z0-9_]{1,29}$")
    steps: str = Field(min_length=20, max_length=20_000)
    fields: list[FunnelField] = Field(min_length=1, max_length=15)
    triggers: list[str] = []


class TenantFunnel(BaseModel):
    funnel_key: str | None


class TenantHours(BaseModel):
    opening_hours: dict | None  # {"sun": ["10:00", "21:00"], ..., "sat": null}, Nepal time; null = always open


class TryIn(BaseModel):
    tenant_id: uuid.UUID
    message: str = Field(min_length=1, max_length=2000)
    history: list[tuple[str, str]] = []  # [("customer" | "assistant", text)], oldest first
    funnel_key: str | None = None  # None = the tenant's own funnel
    version: int | None = None  # None = that funnel's active version


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
            "funnel_key": t.funnel_key,
            "opening_hours": t.opening_hours,
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


@router.get("/activity")
async def activity(
    db: Annotated[AsyncSession, Depends(get_db)], days: Annotated[int, Query(ge=7, le=365)] = 90
) -> list[dict]:
    """The platform's growth, one row per day (Nepal dates) for the last `days` days, all tenants: new
    conversations, messages and AI replies that day, and the running totals of conversations and messages."""
    start = datetime.now(NEPAL).date() - timedelta(days=days - 1)

    async def per_day(model, *where) -> tuple[dict, int]:
        day = func.date(func.timezone("Asia/Kathmandu", model.created_at))
        counts = dict((await db.execute(select(day, func.count()).where(day >= start, *where).group_by(day))).all())
        before = await db.scalar(select(func.count()).select_from(model).where(day < start, *where))
        return counts, before

    convs, total_c = await per_day(Conversation)
    msgs, total_m = await per_day(Message)
    ai, _ = await per_day(Message, Message.sender_type == "ai")
    rows = []
    for n in range(days):
        d = start + timedelta(days=n)
        total_c, total_m = total_c + convs.get(d, 0), total_m + msgs.get(d, 0)
        rows.append({"day": d, "conversations": convs.get(d, 0), "messages": msgs.get(d, 0), "ai_replies": ai.get(d, 0),
                     "total_conversations": total_c, "total_messages": total_m})
    return rows


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


# ---- Conversation funnels (services/funnels.py) ----

def _funnel_out(f: Funnel) -> dict:
    return {"id": f.id, "key": f.key, "version": f.version, "name": f.name, "label": f.label, "steps": f.steps,
            "fields": f.fields, "triggers": f.triggers, "active": f.active, "created_at": f.created_at}


@router.get("/funnels")
async def list_funnels(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    """Every version of every funnel, newest first (the active one is what tenants on that key get)."""
    rows = (await db.execute(select(Funnel).order_by(Funnel.key, Funnel.version.desc()))).scalars()
    return [_funnel_out(f) for f in rows]


@router.post("/funnels", status_code=201)
async def save_funnel(body: FunnelIn, admin: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Save a funnel as a new version of its key and make it the live one (a new key starts at version 1).
    The old version is kept: activate it again to roll back."""
    if "{label}" not in body.steps:
        raise HTTPException(status_code=422, detail="The steps must say where the label goes: write {label} there.")
    if body.label == "summary":
        raise HTTPException(status_code=422, detail='"summary" is the summary label; pick another label name.')
    if unknown := set(body.triggers) - set(funnels.TRIGGERS):
        raise HTTPException(status_code=422, detail=f"Unknown trigger(s): {', '.join(sorted(unknown))}.")
    keys = [f.key for f in body.fields]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="Two fields have the same key.")
    if {"save_order", "website_post"} & set(body.triggers) and not {"name", "phone", "address", "items"} <= set(keys):
        raise HTTPException(status_code=422, detail="save_order and website_post need fields name, phone, address and items.")
    if "website_post" in body.triggers and "save_order" not in body.triggers:
        raise HTTPException(status_code=422, detail="website_post sends the saved order: add save_order before it.")
    last = await db.scalar(select(func.max(Funnel.version)).where(Funnel.key == body.key))
    await db.execute(update(Funnel).where(Funnel.key == body.key).values(active=False))
    funnel = Funnel(**body.model_dump(exclude={"fields"}), fields=[f.model_dump() for f in body.fields],
                    version=(last or 0) + 1, active=True)
    db.add(funnel)
    await db.flush()
    await record_audit(db, tenant_id=None, actor_id=admin.id, actor_type="super_admin", action="funnel_saved",
                       entity_type="funnel", entity_id=funnel.id, meta={"key": funnel.key, "version": funnel.version})
    await db.commit()
    return _funnel_out(funnel)


@router.post("/funnels/{key}/activate/{version}")
async def activate_funnel(key: str, version: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Make an earlier (or later) version of a funnel the live one: a rollback."""
    funnel = await db.scalar(select(Funnel).where(Funnel.key == key, Funnel.version == version))
    if funnel is None:
        raise HTTPException(status_code=404, detail="Funnel version not found")
    await db.execute(update(Funnel).where(Funnel.key == key).values(active=False))
    funnel.active = True
    await db.commit()
    return _funnel_out(funnel)


@router.patch("/tenants/{tenant_id}/funnel")
async def set_tenant_funnel(
    tenant_id: uuid.UUID, body: TenantFunnel, admin: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    """Which funnel the tenant's bot follows; null = the built-in order steps."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if body.funnel_key and not await db.scalar(select(Funnel.id).where(Funnel.key == body.funnel_key, Funnel.active.is_(True))):
        raise HTTPException(status_code=422, detail=f"No live funnel {body.funnel_key!r}.")
    tenant.funnel_key = body.funnel_key
    await record_audit(db, tenant_id=tenant.id, actor_id=admin.id, actor_type="super_admin", action="funnel_set",
                       entity_type="tenant", entity_id=tenant.id, meta={"funnel_key": body.funnel_key})
    await db.commit()
    return {"id": tenant.id, "funnel_key": tenant.funnel_key}


@router.patch("/tenants/{tenant_id}/hours")
async def set_tenant_hours(
    tenant_id: uuid.UUID, body: TenantHours, admin: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict:
    """When the tenant's bot may take orders. While closed it answers questions but not orders (services/hours.py)."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if body.opening_hours is not None and not hours.valid(body.opening_hours):
        raise HTTPException(status_code=422, detail='Use {"sun": ["10:00", "21:00"], ..., "sat": null}: days sun..sat, 24-hour HH:MM.')
    tenant.opening_hours = body.opening_hours
    await record_audit(db, tenant_id=tenant.id, actor_id=admin.id, actor_type="super_admin", action="opening_hours_set",
                       entity_type="tenant", entity_id=tenant.id, meta={"opening_hours": body.opening_hours})
    await db.commit()
    return {"id": tenant.id, "opening_hours": tenant.opening_hours, "open_now": hours.is_open(tenant.opening_hours)}


@router.post("/funnels/try")
async def try_funnel(body: TryIn, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """What a tenant's bot would answer, under its own funnel or one being tried. Nothing is sent or saved."""
    tenant = await db.get(Tenant, body.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    funnel = None
    if body.funnel_key:
        q = select(Funnel).where(Funnel.key == body.funnel_key)
        q = q.where(Funnel.version == body.version) if body.version else q.where(Funnel.active.is_(True))
        funnel = await db.scalar(q)
        if funnel is None:
            raise HTTPException(status_code=404, detail="Funnel version not found")
    try:
        reply = await preview_reply(db, tenant, body.message, body.history, funnel)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="The AI service is not responding. Try again.")
    if reply is None:
        raise HTTPException(status_code=409, detail="This tenant has no shop information yet.")
    return {"reply": reply, "funnel": f"{(funnel or await funnels.for_tenant(db, tenant)).key}"}


@router.get("/funnels/submissions")
async def list_submissions(
    db: Annotated[AsyncSession, Depends(get_db)], tenant_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    """What the funnels collected, newest first, optionally for one tenant."""
    q = select(FunnelSubmission).order_by(FunnelSubmission.created_at.desc()).limit(limit)
    if tenant_id:
        q = q.where(FunnelSubmission.tenant_id == tenant_id)
    return [
        {"id": s.id, "tenant_id": s.tenant_id, "conversation_id": s.conversation_id, "funnel": s.funnel_key,
         "version": s.funnel_version, "fields": s.fields, "status": s.status, "result": s.result, "created_at": s.created_at}
        for s in (await db.execute(q)).scalars()
    ]
