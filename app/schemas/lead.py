"""Sales lead / order request and response schemas."""
import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

LeadStatus = Literal["new", "confirmed", "cancelled"]


class LeadOut(BaseModel):
    id: uuid.UUID
    status: str
    phone: str
    channel: str
    customer_name: Optional[str] = None  # the chat profile name; the order's own name is in `order`
    conversation_id: Optional[uuid.UUID] = None
    order: Optional[dict] = None  # {name, phone, address, items} once the bot collected an order
    kitchen: Optional[dict] = None  # set once this order is in the business's order system
    kitchen_error: Optional[str] = None  # the last automatic send that failed
    updated_at: datetime


class LeadPatch(BaseModel):
    status: LeadStatus
