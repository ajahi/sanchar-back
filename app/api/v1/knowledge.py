"""Shop context — the facts the auto-reply bot answers from, written by the business owner.

Topics are the owner's own: one knowledge_documents row per topic (title = topic name, content = the
answers). A consultancy writes "Courses" and "Countries", a nail salon "Services & prices"; nothing is
fixed. The bot reads every topic as plain text (services/reply_pipeline.build_knowledge), so what is
typed here is exactly what customers can be told. Owner/admin only, always scoped to the caller's tenant.
"""
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.knowledge import KnowledgeDocument
from app.schemas.knowledge import KnowledgeOut, Suggestion, TestIn, TestOut, TopicIn, TopicOut
from app.services.audit import record_audit
from app.services.auto_reply import build_system_prompt, generate_reply, photo_hint, split_photos
from app.services.reply_pipeline import build_knowledge, sendable_photos

router = APIRouter(prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(require_role("owner", "admin"))])

# ponytail: one text blob per tenant in the prompt; past ~30K chars a tenant needs retrieval, not a bigger prompt.
MAX_CHARS = 30_000

# Starter topics per kind of business, offered as one-tap suggestions. Only a head start: owners add any
# topic they like. The hint becomes the topic's placeholder, so they see what a good answer looks like.
STARTERS: dict[str, list[tuple[str, str]]] = {
    "General": [
        ("About the business", "What you do and where you are. e.g. We sell handmade Nepali clothing online from Kathmandu."),
        ("Opening hours & contact", "e.g. 10 AM - 7 PM, Sunday to Friday. Closed Saturday. Phone +977 98XXXXXXXX."),
        ("Payment", "Accepted methods and any limits. e.g. Cash on delivery inside the valley only. eSewa, Khalti, bank transfer."),
        ("Discounts & offers", "State standing offers exactly, or say none. The bot never negotiates. e.g. 10% off orders of 3 or more."),
    ],
    "Clothing": [
        ("Products & prices", "One product per line: name - price - sizes/colours - stock.\ne.g. Pashmina Shawl - Rs 4,500 - maroon, navy - in stock"),
        ("Sizes & fit", "e.g. Kurtas come in S-XXL. Runs small: order one size up. Measurements on request."),
        ("Delivery", "Areas, charges, how long. e.g. Kathmandu Valley Rs 100, same day before 2 PM. Outside valley Rs 250, 2-4 days."),
        ("Returns & exchange", "e.g. Exchange within 7 days if unused with tags. No cash refunds."),
    ],
    "Gifts": [
        ("Gift categories & prices", "One line per category. e.g. Hampers Rs 1,500-5,000. Personalised mugs Rs 650. Flowers from Rs 1,200."),
        ("Customisation & wrapping", "e.g. Name printing on mugs and frames, 2 days. Gift wrap Rs 100. Handwritten card free."),
        ("Delivery & occasions", "e.g. Same-day delivery in Kathmandu if ordered before 1 PM. Midnight delivery Rs 500 extra."),
    ],
    "Consultancy": [
        ("Courses & programs", "One per line: course - level - duration - intake. e.g. MBA - Masters - 2 years - Feb, Sep intakes."),
        ("Countries & universities", "e.g. Australia, UK, Canada, Japan. Partner universities: ... We do not process US visas."),
        ("Fees & documents", "Consultancy fee, what it covers, documents needed. e.g. Rs 25,000, includes visa filing. Need passport, transcripts, IELTS."),
        ("Process & timeline", "Steps from first visit to visa. e.g. Counselling > application (2-4 weeks) > offer letter > visa (4-8 weeks)."),
    ],
    "Nail salon": [
        ("Services & prices", "One per line: service - price - time. e.g. Gel manicure - Rs 1,500 - 45 min. Nail art from Rs 200 per nail."),
        ("Booking & timings", "How to book, walk-ins, cancellations. e.g. Book by DM or phone. Walk-ins if free. 11 AM - 8 PM daily."),
        ("Location & parking", "e.g. Jhamsikhel, opposite the bakery, first floor. Two-wheeler parking only."),
    ],
}
SUGGESTIONS = {kind: [Suggestion(title=t, hint=h) for t, h in items] for kind, items in STARTERS.items()}


def _out(doc: KnowledgeDocument) -> TopicOut:
    return TopicOut(id=doc.id, title=doc.title, content=doc.content or "", hint=(doc.meta or {}).get("hint", ""))


def _title(body: TopicIn) -> str:
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Give the topic a title.")
    return title


async def _own(db: AsyncSession, tenant_id, topic_id: uuid.UUID) -> KnowledgeDocument:
    doc = await db.get(KnowledgeDocument, topic_id)
    if doc is None or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic not found")
    return doc


async def _check_size(db: AsyncSession, tenant_id, new_len: int, skip: uuid.UUID | None = None) -> None:
    """All topics together go into one prompt, so the cap is on the total, not per topic."""
    q = select(func.coalesce(func.sum(func.length(KnowledgeDocument.content)), 0)).where(KnowledgeDocument.tenant_id == tenant_id)
    if skip is not None:
        q = q.where(KnowledgeDocument.id != skip)
    total = (await db.scalar(q)) + new_len
    if total > MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Shop info is too long ({total:,} of {MAX_CHARS:,} characters). Shorten a topic.",
        )


@router.get("", response_model=KnowledgeOut)
async def get_knowledge(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> KnowledgeOut:
    docs = (
        await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant.id).order_by(KnowledgeDocument.created_at)
        )
    ).scalars()
    return KnowledgeOut(topics=[_out(d) for d in docs], suggestions=SUGGESTIONS, ai_enabled=tenant.ai_auto_reply, max_chars=MAX_CHARS)


@router.post("", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
async def add_topic(
    body: TopicIn, tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> TopicOut:
    content = body.content.strip()
    await _check_size(db, tenant.id, len(content))
    doc = KnowledgeDocument(
        tenant_id=tenant.id, title=_title(body), document_type="topic", content=content,
        meta={"hint": body.hint.strip()} if body.hint.strip() else {},
    )
    db.add(doc)
    await db.flush()
    await record_audit(db, tenant_id=tenant.id, actor_id=user.id, action="knowledge_topic_added",
                       entity_type="knowledge_document", entity_id=doc.id, meta={"title": doc.title})
    await db.commit()
    return _out(doc)


@router.put("/{topic_id}", response_model=TopicOut)
async def save_topic(
    topic_id: uuid.UUID, body: TopicIn, tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> TopicOut:
    doc = await _own(db, tenant.id, topic_id)
    content = body.content.strip()
    await _check_size(db, tenant.id, len(content), skip=doc.id)
    # An empty topic stays (the bot skips it) so the owner can fill it later; Delete removes it.
    doc.title, doc.content, doc.status = _title(body), content, "active"
    await record_audit(db, tenant_id=tenant.id, actor_id=user.id, action="knowledge_updated",
                       entity_type="knowledge_document", entity_id=doc.id, meta={"title": doc.title, "chars": len(content)})
    await db.commit()
    return _out(doc)


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(
    topic_id: uuid.UUID, tenant: CurrentTenant, user: CurrentUser, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    doc = await _own(db, tenant.id, topic_id)
    await record_audit(db, tenant_id=tenant.id, actor_id=user.id, action="knowledge_topic_deleted",
                       entity_type="knowledge_document", entity_id=doc.id, meta={"title": doc.title})
    await db.delete(doc)
    await db.commit()


@router.post("/test", response_model=TestOut)
async def test_bot(
    body: TestIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> TestOut:
    """What the bot would answer a customer right now, from the saved shop context. Nothing is sent or stored."""
    knowledge = await build_knowledge(db, tenant.id)
    if not knowledge:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Add some shop information first.")
    photos = await sendable_photos(db, tenant.id)
    try:
        listed = [(p.title, p.tags) for p in photos]
        reply = await generate_reply(
            build_system_prompt(knowledge, photos=listed), [], body.message, photo_hint(body.message, listed)
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The AI service is not responding. Try again.")
    # Nothing is sent here: name the photos the bot would attach, so the admin can check its picks.
    reply, picks = split_photos(reply, len(photos))
    shown = [f"[would send photo: {photos[i].title or 'untitled'}]" for i in picks]
    return TestOut(reply="\n".join(x for x in [reply, *shown] if x))
