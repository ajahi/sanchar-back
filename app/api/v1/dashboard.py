"""Dashboard — per-channel counts for the caller's tenant, in one call.

  new_messages      customer messages in the window
  top_queries       most asked customer questions in the window (see _is_question)
  messages_handled  replies sent by the AI or an agent in the window
  handover          open conversations currently with a human (mode != "ai"), no window
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.dashboard import DashboardOut, TopQuery

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

CHANNELS = ("whatsapp", "instagram", "facebook")

# ponytail: keyword heuristic for "is this a question" (English, Nepglish, Devanagari);
# swap for LLM intent classification if it lets greetings through or misses real questions.
_QUESTION_WORDS = (
    r"\m(what|how|why|when|where|which|who|price|rate|pp|available|deliver|delivery|"
    r"kati|kasari|kaha|kahile|kina|kun|milcha|paincha)\M"
    r"|^\s*(do|does|did|can|could|is|are|will|would|should|may|have|has)\M"
    r"|(कति|कहाँ|किन|कसरी|कुन|कहिले)"
)


def _is_question(content):
    return content.contains("?") | content.contains("？") | content.op("~*")(_QUESTION_WORDS)


async def _by_channel(db: AsyncSession, stmt) -> dict[str, int]:
    """Run a (channel, count) query; every known channel present, 0 when missing."""
    counts = dict.fromkeys(CHANNELS, 0)
    counts.update({channel: n for channel, n in (await db.execute(stmt)).all()})
    return counts


@router.get("", response_model=DashboardOut)
async def dashboard(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=90)] = 1,
) -> DashboardOut:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    msgs = (
        select(Conversation.channel, func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.tenant_id == tenant.id, Message.created_at >= since)
        .group_by(Conversation.channel)
    )

    # ponytail: exact-text grouping (only lower/trim), so "Do you deliver?" and "u deliver?"
    # count apart; cluster by meaning (embeddings/LLM) if near-duplicates split the list.
    # Most asked first; ties go to the most recently asked.
    key = func.lower(func.trim(Message.content))
    top = await db.execute(
        select(func.min(Message.content), func.count())
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.tenant_id == tenant.id,
            Message.created_at >= since,
            Message.sender_type == "customer",
            _is_question(Message.content),
        )
        .group_by(key)
        .order_by(func.count().desc(), func.max(Message.created_at).desc())
        .limit(3)
    )

    return DashboardOut(
        window_days=days,
        new_messages=await _by_channel(db, msgs.where(Message.sender_type == "customer")),
        top_queries=[TopQuery(text=t, count=n) for t, n in top.all()],
        messages_handled=await _by_channel(db, msgs.where(Message.sender_type.in_(("ai", "agent")))),
        handover=await _by_channel(
            db,
            select(Conversation.channel, func.count())
            .where(
                Conversation.tenant_id == tenant.id,
                Conversation.status == "open",
                Conversation.mode != "ai",
            )
            .group_by(Conversation.channel),
        ),
    )
