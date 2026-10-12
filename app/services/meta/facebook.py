"""Facebook Messenger (Pages) — send and read through a Page access token.

No OAuth hop here: the Channels page links a Page by its id + Page access token
(POST /social-accounts/facebook), stored encrypted on social_accounts
(platform="facebook", external_account_id=page_id). Inbound messages arrive on the shared Meta
webhook (webhooks.py, object "page"); same event shape as Instagram, signed with META_APP_SECRET.
Function names and return shapes match instagram.py so the inbox and reply pipeline treat both alike.
"""
import logging

import httpx

from app.services.http_client import SSL_CTX
from app.services.meta.instagram import API_VERSION

GRAPH_BASE = "https://graph.facebook.com"
# messages = customer DMs; message_echoes = replies sent from the Page inbox (pauses the bot).
SUBSCRIBED_FIELDS = "messages,message_echoes"

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}  # header, so the token isn't in logged URLs


async def fetch_page(access_token: str) -> dict:
    """{id, name} of the Page the token belongs to — also proves the token works.
    A user token answers with the user's own id, which link_facebook rejects."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/me", params={"fields": "id,name"}, headers=_auth(access_token)
        )
        resp.raise_for_status()
        return resp.json()


async def subscribe_page(access_token: str, page_id: str) -> None:
    """Have Meta deliver this Page's Messenger webhooks to our app. Without it the Page sends fine
    but its customers' messages never reach us. Repeating it is harmless."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{page_id}/subscribed_apps",
            params={"subscribed_fields": SUBSCRIBED_FIELDS},
            headers=_auth(access_token),
        )
        resp.raise_for_status()


async def fetch_subscribed_fields(access_token: str, page_id: str) -> list[str]:
    """Webhook fields Meta delivers for this Page to our app. Empty = no webhooks."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/{page_id}/subscribed_apps", headers=_auth(access_token)
        )
        resp.raise_for_status()
        return [f for app in resp.json().get("data", []) for f in app.get("subscribed_fields", [])]


async def _send(access_token: str, page_id: str, recipient_psid: str, message: dict) -> dict:
    # RESPONSE = a reply inside the 24h window after the customer's last message; outside it Meta
    # answers 400 (code 10 / subcode 2018278), which the inbox surfaces as a 502 with Meta's text.
    body = {"recipient": {"id": recipient_psid}, "messaging_type": "RESPONSE", "message": message}
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{page_id}/messages", json=body, headers=_auth(access_token)
        )
        resp.raise_for_status()
        return resp.json()


async def send_text(access_token: str, page_id: str, recipient_psid: str, text: str) -> dict:
    """Send a text message from the Page. Returns {recipient_id, message_id}."""
    return await _send(access_token, page_id, recipient_psid, {"text": text})


async def send_image(access_token: str, page_id: str, recipient_psid: str, image_url: str) -> dict:
    """Send an image by public URL (Meta fetches it). Returns {recipient_id, message_id}."""
    return await _send(
        access_token, page_id, recipient_psid, {"attachment": {"type": "image", "payload": {"url": image_url}}}
    )


async def fetch_recent_conversations(access_token: str, limit: int = 5) -> list[dict]:
    """Newest `limit` Messenger threads with their recent messages (a Page token makes /me the Page)."""
    params = {
        "platform": "messenger",
        "fields": "id,updated_time,participants,messages{id,created_time,from,message,attachments}",
        "limit": limit,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/me/conversations", params=params, headers=_auth(access_token)
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def fetch_customer_profile(access_token: str, psid: str) -> dict:
    """Best-effort {name} of a Messenger user (no username on Messenger); {} on any failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/{API_VERSION}/{psid}", params={"fields": "name"}, headers=_auth(access_token)
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        body = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        log.warning("facebook profile fetch failed for psid=%s: %s", psid, body)
        return {}
