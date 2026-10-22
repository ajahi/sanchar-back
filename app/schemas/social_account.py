"""Connected channel accounts: Instagram profile (instagram_business_basic), WhatsApp number, Facebook Page."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


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


class WhatsAppLinkIn(BaseModel):
    """Channels page Connect form. Digits only: both ids go straight into Graph URLs."""

    phone_number_id: str = Field(pattern=r"^\d{5,30}$")
    waba_id: str = Field(pattern=r"^\d{5,30}$")


class WhatsAppSignupIn(BaseModel):
    """Connect WhatsApp button: the one-time code Meta's Embedded Signup popup hands the browser."""

    code: str = Field(min_length=10, max_length=2000)


class FacebookPageOut(BaseModel):
    """One linked Facebook Page + the live checks the Channels page's PING shows."""

    page_id: str
    name: Optional[str] = None
    subscribed_fields: Optional[list[str]] = None  # webhook fields Meta delivers; None = unknown, [] = none
    last_webhook_at: Optional[datetime] = None  # when Meta last reached us for this Page
    linked_instagram_id: Optional[str] = None  # the Instagram account Meta links to this Page, if any
    instagram_verified: bool = False  # that Instagram account is connected to this same workspace
    connected_at: datetime
    live: bool  # False = Meta didn't answer (revoked/expired token…); only stored values shown


class FacebookLinkIn(BaseModel):
    """Channels page Connect form: the Page id and a Page access token (never a user token)."""

    page_id: str = Field(pattern=r"^\d{5,30}$")
    page_access_token: str = Field(min_length=20, max_length=1000)
