"""Connected Instagram account profile (instagram_business_basic)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class InstagramProfileOut(BaseModel):
    id: str  # Instagram user ID
    username: Optional[str] = None
    name: Optional[str] = None
    account_type: Optional[str] = None
    profile_picture_url: Optional[str] = None
    followers_count: Optional[int] = None
    follows_count: Optional[int] = None
    media_count: Optional[int] = None
    connected_at: datetime
    live: bool  # False = Instagram didn't answer (e.g. expired token); only stored id/username shown
