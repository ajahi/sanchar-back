"""Connected channel accounts: Instagram profile (instagram_business_basic), WhatsApp number."""
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


class WhatsAppAccountOut(BaseModel):
    """One WhatsApp number + the three live checks the Channels page's PING shows."""

    phone_number_id: str
    display_phone_number: Optional[str] = None
    verified_name: Optional[str] = None
    status: Optional[str] = None  # Meta: CONNECTED = token + number can send
    quality_rating: Optional[str] = None  # GREEN / YELLOW / RED
    waba_id: Optional[str] = None  # learned from the first webhook event
    subscribed_apps: Optional[list[str]] = None  # apps Meta delivers webhooks to; None = unknown
    last_webhook_at: Optional[datetime] = None  # when Meta last reached us for this number
    connected_at: datetime
    live: bool  # False = Meta didn't answer; only stored values shown
