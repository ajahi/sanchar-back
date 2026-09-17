"""Inbox schemas: conversation list, message list, reply."""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ConversationOut(BaseModel):
    id: uuid.UUID
    channel: str
    status: str
    mode: str
    last_message_at: Optional[datetime] = None
    customer_id: uuid.UUID
    customer_name: Optional[str] = None
    customer_username: Optional[str] = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sender_type: str
    message_type: str
    content: Optional[str] = None
    media_url: Optional[str] = None
    ai_generated: bool
    created_at: datetime


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)  # Instagram text DM cap
