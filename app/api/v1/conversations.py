"""Inbox — list a tenant's conversations, read a thread, reply as an agent over Instagram."""
import uuid
from datetime import datetime, timezone
from typing import Annotated

import httpx
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
from app.services.meta import instagram

router = APIRouter(prefix="/conversations", tags=["conversations"])


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
    status_: Annotated[str, Query(alias="status")] = "open",
) -> list[ConversationOut]:
    rows = await db.execute(
        select(Conversation, Customer)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.tenant_id == tenant.id, Conversation.status == status_)
        .order_by(Conversation.last_message_at.desc().nulls_last())
    )
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
) -> list[Message]:
    await _own_conversation(db, tenant.id, conversation_id)
    rows = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
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
    try:
        sent = await instagram.send_text(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            body.text,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.response.text[:500])
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Instagram unreachable")

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
