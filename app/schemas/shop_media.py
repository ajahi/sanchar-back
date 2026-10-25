"""Shop media request/response schemas."""
import uuid
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TAGS = 8


def clean_tags(tags: list[str]) -> list[str]:
    """Lowercase, single-spaced, no '#', at most 30 chars, no duplicates, at most MAX_TAGS (order kept)."""
    out: list[str] = []
    for t in tags:
        t = " ".join(str(t).lower().split()).lstrip("#").strip()[:30].strip()
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


class ShopMediaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    url: str
    media_type: str
    title: Optional[str] = None
    tags: list[str] = []
    caption: Optional[str] = None
    permalink: Optional[str] = None
    ig_media_id: Optional[str] = None  # the Instagram post id (imported posts, and photos posted from Sanchar)
    in_stock: bool
    campaign_id: Optional[uuid.UUID] = None
    sort_order: int = 0


class ShopMediaPatch(BaseModel):
    title: Optional[str] = Field(default=None, max_length=255)
    tags: Optional[list[str]] = Field(default=None, max_length=30)  # replaces the list; cleaned to MAX_TAGS
    caption: Optional[str] = Field(default=None, max_length=2000)
    in_stock: Optional[bool] = None
    campaign_id: Optional[uuid.UUID] = None  # null takes the photo out of its campaign
    sort_order: Optional[int] = Field(default=None, ge=0, le=1000)

    @field_validator("tags")
    @classmethod
    def _clean(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return None if v is None else clean_tags(v)


class TagSuggestion(BaseModel):
    """The vision model's guess for one photo; nothing is saved until the admin confirms it with PATCH."""

    title: Optional[str] = None
    tags: list[str] = []


class PostIn(BaseModel):
    caption: str = Field(default="", max_length=2200)  # Instagram's caption limit


class SendMediaIn(BaseModel):
    media_id: uuid.UUID
