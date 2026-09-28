"""WhatsApp Cloud API — send from a registered business number.

No OAuth hop like Instagram: auth is a System User token, stored encrypted on the
social_accounts row (platform="whatsapp", external_account_id=phone_number_id) by
scripts/connect_whatsapp.py. Inbound messages arrive on the shared Meta webhook (webhooks.py).
"""
import httpx

from app.services.meta.instagram import API_VERSION

GRAPH_BASE = "https://graph.facebook.com"

_TIMEOUT = httpx.Timeout(15.0)


async def fetch_phone_number(access_token: str, phone_number_id: str) -> dict:
    """{display_phone_number, verified_name} of the business number — also proves the token works."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}",
            params={"fields": "display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def send_text(access_token: str, phone_number_id: str, to_wa_id: str, text: str) -> dict:
    """Send a free-form text. Returns {message_id}, same shape as instagram.send_text.

    Only delivered inside the 24h window after the customer's last message. Outside it Meta still
    answers 200; the failure (code 131047) arrives later as a `statuses` webhook.
    """
    body = {"messaging_product": "whatsapp", "to": to_wa_id, "type": "text", "text": {"body": text}}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}/messages",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return {"message_id": resp.json()["messages"][0]["id"]}
