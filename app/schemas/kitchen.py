"""Schemas for a business's own online-order system (services/kitchen.py)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class KitchenOut(BaseModel):
    configured: bool
    base_url: Optional[str] = None
    email: Optional[str] = None
    send_enabled: bool = False
    auto_send: bool = False
    synced_at: Optional[datetime] = None
    item_count: int = 0
    location_count: int = 0


class KitchenIn(BaseModel):
    base_url: str = Field(min_length=8, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    send_enabled: bool = False
    auto_send: bool = False


class MenuItem(BaseModel):
    id: str
    name: str
    price: int
    category: str


class Area(BaseModel):
    address: str
    rate: int


class CatalogOut(BaseModel):
    items: list[MenuItem]
    areas: list[Area]


class Line(BaseModel):
    text: Optional[str] = None  # the order line as the bot wrote it
    product_id: Optional[str] = None  # None: matched nothing, staff must pick
    quantity: int = Field(default=1, ge=1, le=99)


class KitchenPlan(BaseModel):
    lines: list[Line]
    location: Optional[str] = None  # an area from the list, "Pick up", or None (staff must pick)
    send_enabled: bool
    sent: Optional[dict] = None  # set once this order is in the kitchen: kitchen_order_id, total, ...
    error: Optional[str] = None  # the last automatic send that failed
    body: Optional[dict] = None  # exactly what would be posted, when every line and the area are known


class SendIn(BaseModel):
    lines: list[Line] = Field(min_length=1, max_length=30)
    location: str = Field(min_length=1, max_length=255)
