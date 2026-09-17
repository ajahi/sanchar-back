"""Schemas for connected social accounts and their import runs."""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class SocialAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    platform: str
    account_name: Optional[str] = None
    external_account_id: str
    external_page_id: Optional[str] = None
    status: str
    token_expires_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None


class SyncRunOut(BaseModel):
    """One historical-import attempt; `status` is pending/running/succeeded/partial/failed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    social_account_id: uuid.UUID
    status: str
    trigger_source: str
    conversations_seen: int
    conversations_created: int
    conversations_failed: int
    messages_created: int
    messages_skipped: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    created_at: datetime
