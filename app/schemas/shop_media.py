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
    ig_media_id: Optional[str] = None  # the Instagram post id (imported posts, and photos posted from Sanchar)
    in_stock: bool
    campaign_id: Optional[uuid.UUID] = None
    sort_order: int = 0


class ShopMediaPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    caption: Optional[str] = Field(default=None, max_length=2000)
    in_stock: Optional[bool] = None
    campaign_id: Optional[uuid.UUID] = None  # null takes the photo out of its campaign
    sort_order: Optional[int] = Field(default=None, ge=0, le=1000)


class PostIn(BaseModel):
    caption: str = Field(default="", max_length=2200)  # Instagram's caption limit


class SendMediaIn(BaseModel):
    media_id: uuid.UUID
