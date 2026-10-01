"""Shop media request/response schemas."""
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ShopMediaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    url: str
    media_type: str
    title: Optional[str] = None
    caption: Optional[str] = None
    permalink: Optional[str] = None
    in_stock: bool


class ShopMediaPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    caption: Optional[str] = Field(default=None, max_length=2000)
    in_stock: Optional[bool] = None


class SendMediaIn(BaseModel):
    media_id: uuid.UUID
