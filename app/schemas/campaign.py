"""Campaign request/response schemas."""
import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    price: Optional[int] = Field(default=None, ge=0, le=100_000_000)  # whole rupees
    blurb: Optional[str] = Field(default=None, max_length=500)  # "pure chinnon silk fabric, comes with quality guarantee"
    details: Optional[str] = Field(default=None, max_length=5000)  # colours, sizes, stock: for the AI's other answers
    status: Literal["active", "paused"] = "active"


class CampaignPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    price: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    blurb: Optional[str] = Field(default=None, max_length=500)
    details: Optional[str] = Field(default=None, max_length=5000)
    status: Optional[Literal["active", "paused"]] = None


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    price: Optional[int] = None
    blurb: Optional[str] = None
    details: Optional[str] = None
    status: str
    photo_count: int = 0
    ig_post_id: Optional[str] = None  # set once it is posted to Instagram from Sanchar
    ig_permalink: Optional[str] = None


class PublishIn(BaseModel):
    media_id: uuid.UUID  # one of the shop's uploaded photos
    caption: str = Field(default="", max_length=2200)  # Instagram's caption limit


class LinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    external_id: str
    ad_title: Optional[str] = None
    ad_image_url: Optional[str] = None
    campaign_id: Optional[uuid.UUID] = None  # None = an ad we have seen but nobody has mapped yet
    created_at: datetime


class LinkIn(BaseModel):
    kind: Literal["ad", "ref"]
    external_id: str = Field(min_length=1, max_length=255)
    campaign_id: Optional[uuid.UUID] = None


class LinkPatch(BaseModel):
    campaign_id: Optional[uuid.UUID] = None  # null unmaps it
