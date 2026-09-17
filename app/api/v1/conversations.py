"""Inbox — list a tenant's conversations, read a thread, reply as an agent over Instagram."""
import uuid
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_token
from app.core.tenant import CurrentTenant, CurrentUser
from app.db.session import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.schemas.conversation import ConversationOut, MessageOut, ReplyIn
from app.services.meta import graph, instagram

router = APIRouter(prefix="/conversations", tags=["conversations"])

# Meta closes the Standard Messaging Window 24h after the customer's last message.
MESSAGING_WINDOW_CLOSED = 1545041


async def _own_conversation(
    db: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> tuple[Conversation, Customer]:
    # Tenant-scoped fetch (spec §17).
    row = (
        await db.execute(
            select(Conversation, Customer)
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return row.tuple()


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    status_: Annotated[
        Optional[str],
        Query(alias="status", description="open | closed; omit for every thread"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationOut]:
    """A tenant's threads, most recently active first.

    Defaults to every status so an imported history is visible immediately; pass
    `status=open` for just the live queue. Bounded by `limit` — an inbox must not issue an
    unbounded query once a business has thousands of threads.
    """
    stmt = (
        select(Conversation, Customer)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.tenant_id == tenant.id)
        .order_by(Conversation.last_message_at.desc().nulls_last())
        .limit(limit)
        .offset(offset)
    )
    if status_:
        stmt = stmt.where(Conversation.status == status_)

    rows = await db.execute(stmt)
    return [
        ConversationOut(
            id=c.id,
            channel=c.channel,
            status=c.status,
            mode=c.mode,
            last_message_at=c.last_message_at,
            customer_id=cu.id,
            customer_name=cu.name,
            customer_username=cu.external_username,
        )
        for c, cu in rows.all()
    ]


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Message]:
    """Oldest-first slice of a thread; imported messages keep their original timestamps."""
    await _own_conversation(db, tenant.id, conversation_id)
    rows = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all())


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def reply(
    conversation_id: uuid.UUID,
    body: ReplyIn,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Message:
    convo, customer = await _own_conversation(db, tenant.id, conversation_id)
    account = (
        await db.get(SocialAccount, convo.social_account_id) if convo.social_account_id else None
    )
    if account is None or not account.access_token_encrypted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Instagram account not connected"
        )

    # Messaging is driven by the Page token, so the Page id is the path root. The account
    # id is only a fallback for rows connected before Page login existed.
    page_id = account.external_page_id or account.external_account_id
    try:
        sent = await instagram.send_text(
            page_id,
            decrypt_token(account.access_token_encrypted),
            customer.external_user_id,
            body.text,
        )
    except graph.GraphError as exc:
        raise _send_error(exc)

    msg = Message(
        conversation_id=convo.id,
        external_message_id=sent.get("message_id"),
        sender_type="agent",
        sender_agent_id=user.id,
        content=body.text,
    )
    db.add(msg)
    convo.last_message_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)
    return msg


def _send_error(exc: graph.GraphError) -> HTTPException:
    """Translate a Graph send failure into an error the dashboard can explain."""
    if exc.code == MESSAGING_WINDOW_CLOSED:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Instagram's 24-hour messaging window has closed for this customer. "
                "They must message you again before a reply can be sent."
            ),
        )
    if exc.is_revoked:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Instagram account's access token is no longer valid; reconnect it.",
        )
    if exc.is_auth_error:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Instagram rejected the request: {exc.message}",
        )
    if exc.is_throttled:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Instagram is rate limiting this account; try again shortly.",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
