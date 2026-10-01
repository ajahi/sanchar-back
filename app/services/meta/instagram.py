"""Instagram API with Instagram Login — OAuth client.

Flow (see Meta docs: Business Login for Instagram):
  1. Send the user to AUTHORIZE_URL.
  2. Exchange the returned `code` for a short-lived token (TOKEN_URL).
  3. Upgrade to a 60-day long-lived token (GRAPH /access_token).
  4. Read the profile (GRAPH /me).
Long-lived tokens are refreshed elsewhere via GRAPH /refresh_access_token.
"""
import hmac
import logging
from hashlib import sha256
from urllib.parse import urlencode

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_BASE = "https://graph.instagram.com"
API_VERSION = "v25.0"  # matches what Meta sends in webhook headers (instagram-api-version)

_TIMEOUT = httpx.Timeout(15.0)


def build_authorize_url(state: str) -> str:
    """Step 1 — the URL to redirect the browser to."""
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_redirect_uri,
        "response_type": "code",
        "scope": settings.instagram_scopes,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    """Step 2 — code -> short-lived token. Returns {access_token, user_id, permissions?}."""
    data = {
        "client_id": settings.instagram_app_id,
        "client_secret": settings.instagram_app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": settings.instagram_redirect_uri,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(TOKEN_URL, data=data)
        resp.raise_for_status()
        return resp.json()


async def exchange_for_long_lived_token(short_lived_token: str) -> dict:
    """Step 3 — short-lived -> 60-day token. Returns {access_token, token_type, expires_in}."""
    params = {
        "grant_type": "ig_exchange_token",
        "client_secret": settings.instagram_app_secret,
        "access_token": short_lived_token,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/access_token", params=params)
        resp.raise_for_status()
        return resp.json()


PROFILE_FIELDS = (
    "user_id,username,name,account_type,profile_picture_url,followers_count,follows_count,media_count"
)


async def fetch_profile(access_token: str, fields: str = "user_id,username") -> dict:
    """Step 4 — the connected account's {user_id, username}; pass PROFILE_FIELDS for the full
    instagram_business_basic profile (picture, name, counts)."""
    params = {"fields": fields, "access_token": access_token}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/me", params=params)
        resp.raise_for_status()
        return resp.json()


async def refresh_long_lived_token(long_lived_token: str) -> dict:
    """Refresh a 60-day token (must be >=24h old). Returns {access_token, token_type, expires_in}."""
    params = {"grant_type": "ig_refresh_token", "access_token": long_lived_token}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/refresh_access_token", params=params)
        resp.raise_for_status()
        return resp.json()


# ---- Messaging (instagram_business_manage_messages) ----


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Check Meta's X-Hub-Signature-256 ("sha256=<hex>") against either of this app's secrets.

    Instagram-Login webhooks are signed with the Instagram app secret, Page-routed ones with
    the Meta app secret; both are ours, so either match proves the request came from Meta.
    """
    return any(
        secret
        and hmac.compare_digest(
            f"sha256={hmac.new(secret.encode(), raw_body, sha256).hexdigest()}", signature_header or ""
        )
        for secret in (settings.instagram_app_secret, settings.meta_app_secret)
    )


async def subscribe_to_messages(access_token: str) -> None:
    """Have Meta deliver this account's DM webhooks to us. Instagram Login needs this once per
    account (the app-level webhook config alone sends nothing); repeating it is harmless."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/me/subscribed_apps",
            params={"subscribed_fields": "messages"},
            headers={"Authorization": f"Bearer {access_token}"},  # header, so the token isn't in logged URLs
        )
        resp.raise_for_status()


async def send_text(access_token: str, ig_account_id: str, recipient_igsid: str, text: str) -> dict:
    """Send a text DM from the business account. Returns {recipient_id, message_id}."""
    body = {"recipient": {"id": recipient_igsid}, "message": {"text": text}}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{ig_account_id}/messages",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def send_image(access_token: str, ig_account_id: str, recipient_igsid: str, image_url: str) -> dict:
    """Send an image DM by public URL (Meta fetches it). Returns {recipient_id, message_id}."""
    body = {
        "recipient": {"id": recipient_igsid},
        "message": {"attachment": {"type": "image", "payload": {"url": image_url}}},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{ig_account_id}/messages",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_media(access_token: str, limit: int = 9) -> list[dict]:
    """The account's newest `limit` posts (needs instagram_business_basic). Image/carousel posts carry
    media_url; videos/reels carry thumbnail_url instead. The urls are signed CDN links that expire."""
    params = {
        "fields": "id,media_type,media_url,thumbnail_url,permalink,caption,timestamp",
        "limit": limit,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/me/media",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def fetch_recent_conversations(access_token: str, limit: int = 5) -> list[dict]:
    """Newest `limit` DM threads with their recent messages (Meta caps at 20 per thread)."""
    params = {
        "platform": "instagram",
        "fields": "id,updated_time,participants,messages{id,created_time,from,message,attachments}",
        "limit": limit,
        "access_token": access_token,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/{API_VERSION}/me/conversations", params=params)
        resp.raise_for_status()
        return resp.json().get("data", [])


async def fetch_customer_profile(access_token: str, igsid: str) -> dict:
    """Best-effort {name, username} of a messaging participant; {} on any failure."""
    params = {"fields": "name,username", "access_token": access_token}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{GRAPH_BASE}/{API_VERSION}/{igsid}", params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        body = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        log.warning("instagram profile fetch failed for igsid=%s: %s", igsid, body)
        return {}
