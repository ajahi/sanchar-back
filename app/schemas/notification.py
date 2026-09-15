"""Notification response schema."""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: Optional[uuid.UUID] = None
    user_id: Optional[uuid.UUID] = None
    source_table: Optional[str] = None
    entity_id: Optional[uuid.UUID] = None
    event: str
    medium: str
    status: str
    subject: Optional[str] = None
    message: Optional[str] = None
    payload: dict
    created_at: datetime
    sent_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
