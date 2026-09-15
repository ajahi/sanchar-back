"""Tenant request/response schemas."""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


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
    created_at: datetime
    updated_at: datetime


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    business_type: Optional[str] = None
    pan_number: Optional[str] = None
    location: Optional[str] = None
    owner_name: Optional[str] = None
    owner_phone: Optional[str] = None
