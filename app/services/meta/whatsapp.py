"""WhatsApp Cloud API — send from a registered business number.

No OAuth hop like Instagram: auth is our platform System User token (WABA_TOKEN), stored
encrypted on the social_accounts row (platform="whatsapp", external_account_id=phone_number_id)
by the Channels Connect form (POST /social-accounts/whatsapp) or scripts/connect_whatsapp.py.
Inbound messages arrive on the shared Meta webhook (webhooks.py).
"""
import httpx

from app.core.config import settings
from app.services.http_client import SSL_CTX
from app.services.meta.instagram import API_VERSION

GRAPH_BASE = "https://graph.facebook.com"

_TIMEOUT = httpx.Timeout(15.0)


# status is CONNECTED when the number can send; quality_rating GREEN/YELLOW/RED.
PHONE_FIELDS = "display_phone_number,verified_name,status,quality_rating"


async def fetch_phone_number(
    access_token: str, phone_number_id: str, fields: str = "display_phone_number,verified_name"
) -> dict:
    """The business number's fields — also proves the token works. Pass PHONE_FIELDS for status."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}",
            params={"fields": fields},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_waba_phone_numbers(access_token: str, waba_id: str) -> list[dict]:
    """[{id, display_phone_number, verified_name}] on a WhatsApp Business Account. Meta answers
    400 (code 100) when the token's business has no access to that account."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/{waba_id}/phone_numbers",
            params={"fields": "id,display_phone_number,verified_name,status"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def fetch_granted_wabas(access_token: str) -> list[str]:
    """Ids of the WhatsApp Business Accounts the business granted this token (Embedded Signup), from
    Meta's own record of the grant, so a browser can't name someone else's account."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/debug_token",
            params={
                "input_token": access_token,
                "access_token": f"{settings.facebook_app_id}|{settings.meta_app_secret}",
            },
        )
        resp.raise_for_status()
        scopes = resp.json().get("data", {}).get("granular_scopes", [])
    return next((s.get("target_ids", []) for s in scopes if s.get("scope") == "whatsapp_business_management"), [])


async def register_number(access_token: str, phone_number_id: str, pin: str) -> None:
    """Turn on Cloud API for a number new to it (added in Embedded Signup); `pin` becomes its
    two-step verification PIN."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}/register",
            json={"messaging_product": "whatsapp", "pin": pin},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()


async def subscribe_app(access_token: str, waba_id: str) -> None:
    """Have Meta deliver this WhatsApp account's webhooks to our app (the token's app).
    Without it a linked number sends fine but its customers' messages never reach us.
    Repeating it is harmless."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()


async def fetch_subscribed_apps(access_token: str, waba_id: str) -> list[str]:
    """Names of the apps Meta delivers this WhatsApp account's webhooks to. Empty = no webhooks."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return [
            (row.get("whatsapp_business_api_data") or {}).get("name", "?")
            for row in resp.json().get("data", [])
        ]


async def send_image(access_token: str, phone_number_id: str, to_wa_id: str, image_url: str) -> dict:
    """Send an image by public link (Meta fetches it). Returns {message_id}. Same 24h window as text."""
    body = {"messaging_product": "whatsapp", "to": to_wa_id, "type": "image", "image": {"link": image_url}}
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}/messages",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return {"message_id": resp.json()["messages"][0]["id"]}


async def send_text(access_token: str, phone_number_id: str, to_wa_id: str, text: str) -> dict:
    """Send a free-form text. Returns {message_id}, same shape as instagram.send_text.

    Only delivered inside the 24h window after the customer's last message. Outside it Meta still
    answers 200; the failure (code 131047) arrives later as a `statuses` webhook.
    """
    body = {"messaging_product": "whatsapp", "to": to_wa_id, "type": "text", "text": {"body": text}}
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=SSL_CTX) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}/messages",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return {"message_id": resp.json()["messages"][0]["id"]}
