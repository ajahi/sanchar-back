"""Tenant request/response schemas."""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    business_type: Optional[str] = None
    pan_number: Optional[str] = None
    location: Optional[str] = None
    owner_name: Optional[str] = None
    owner_phone: Optional[str] = None
    status: str
    ai_auto_reply: bool
    created_at: datetime
    updated_at: datetime


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    business_type: Optional[str] = None
    pan_number: Optional[str] = None
    location: Optional[str] = None
    owner_name: Optional[str] = None
    owner_phone: Optional[str] = None


class CloseConditionsIn(BaseModel):
    """Close the shop now. for_today: until midnight Nepal time; else until `until`, or until reopened when None."""
    message: Optional[str] = Field(None, max_length=500)  # what the bot tells customers; None = the default
    for_today: bool = False
    until: Optional[datetime] = None  # without a time zone it is Nepal time


class CloseConditionsOut(BaseModel):
    closed: bool
    message: Optional[str] = None  # what the bot tells customers right now (None while open)
    custom_message: Optional[str] = None  # the owner's own message, kept after reopening to reuse next time
    until: Optional[datetime] = None  # None while closed = until reopened
    set_at: Optional[datetime] = None
