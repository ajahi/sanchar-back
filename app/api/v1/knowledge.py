"""Shop context — the facts the auto-reply bot answers from, written by the business owner.

One knowledge_documents row per guided section (document_type = section key). The bot reads them
all as plain text (services/reply_pipeline.build_knowledge), so what is typed here is exactly what
customers can be told. Owner/admin only, always scoped to the caller's tenant.
"""
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.knowledge import KnowledgeDocument
from app.schemas.knowledge import KnowledgeOut, SectionIn, SectionOut, TestIn, TestOut
from app.services.audit import record_audit
from app.services.auto_reply import build_system_prompt, generate_reply
from app.services.reply_pipeline import build_knowledge

router = APIRouter(prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(require_role("owner", "admin"))])

# ponytail: one text blob per tenant in the prompt; past ~30K chars a tenant needs retrieval, not a bigger prompt.
MAX_CHARS = 30_000

# (key, label, hint) — the hint is shown as the placeholder, so owners see what a good entry looks like.
SECTIONS = [
    ("about", "About the shop", "What you sell and where you are. e.g. We sell handmade Nepali clothing online from Kathmandu."),
    (
        "products",
        "Products & prices",
        "One product per line: name - price - sizes/colors - stock.\ne.g. Pashmina Shawl - Rs 4,500 - maroon, navy - in stock\nSilver Earrings - Rs 2,800 - out of stock, back in 2 weeks",
    ),
    ("delivery", "Delivery", "Areas, charges, how long, free-delivery rules. e.g. Kathmandu Valley Rs 100, same day before 2 PM. Outside valley Rs 250, 2-4 days."),
    ("payment", "Payment", "Accepted methods and any limits. e.g. Cash on delivery inside the valley only. eSewa, Khalti, bank transfer."),
    ("returns", "Returns & refunds", "Your policy in plain words. e.g. 7 days for unused items. Refunds are decided by the owner."),
    ("offers", "Discounts & offers", "State standing offers exactly, or say none. The bot never negotiates. e.g. No discounts. 10% off orders of 3 or more items."),
    ("hours", "Opening hours & contact", "e.g. 10 AM - 7 PM, Sunday to Friday. Closed Saturday. Phone +977 98XXXXXXXX."),
]
_BY_KEY = {k: (label, hint) for k, label, hint in SECTIONS}


async def _docs(db: AsyncSession, tenant_id) -> dict[str, KnowledgeDocument]:
    rows = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.document_type.in_(_BY_KEY)
            )
        )
    ).scalars()
    return {d.document_type: d for d in rows}


@router.get("", response_model=KnowledgeOut)
async def get_knowledge(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> KnowledgeOut:
    docs = await _docs(db, tenant.id)
    return KnowledgeOut(
        sections=[
            SectionOut(key=k, label=label, hint=hint, content=(docs[k].content or "") if k in docs else "")
            for k, label, hint in SECTIONS
        ],
        ai_enabled=tenant.ai_auto_reply,
        max_chars=MAX_CHARS,
    )


@router.put("/{key}", response_model=SectionOut)
async def save_section(
    key: str,
    body: SectionIn,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SectionOut:
    if key not in _BY_KEY:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown section")
    label, hint = _BY_KEY[key]
    content = body.content.strip()
    docs = await _docs(db, tenant.id)
    others = sum(len(d.content or "") for k, d in docs.items() if k != key)
    if others + len(content) > MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Shop context is too long ({others + len(content):,} of {MAX_CHARS:,} characters). Shorten a section.",
        )

    doc = docs.get(key)
    if not content:  # clearing a section removes it, so the bot stops using it
        if doc is not None:
            await db.delete(doc)
    elif doc is None:
        db.add(KnowledgeDocument(tenant_id=tenant.id, title=label, document_type=key, content=content))
    else:
        doc.content, doc.status = content, "active"
    await record_audit(
        db, tenant_id=tenant.id, actor_id=user.id, action="knowledge_updated",
        entity_type="knowledge_document", meta={"section": key, "chars": len(content)},
    )
    await db.commit()
    return SectionOut(key=key, label=label, hint=hint, content=content)


@router.post("/test", response_model=TestOut)
async def test_bot(
    body: TestIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> TestOut:
    """What the bot would answer a customer right now, from the saved shop context. Nothing is sent or stored."""
    knowledge = await build_knowledge(db, tenant.id)
    if not knowledge:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Add some shop information first.")
    system = build_system_prompt(tenant.name, knowledge, contact=tenant.owner_phone or "")
    try:
        result = await generate_reply(system, [], body.message)
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The AI service is not responding. Try again.")
    return TestOut(reply=result.reply, handover=result.handover, reason=result.reason)
