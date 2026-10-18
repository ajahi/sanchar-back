"""Mock Meta webhooks for every way a chat can open, then show which reference code each one left.

Posts signed Instagram / Messenger / WhatsApp payloads (the shapes in Meta's webhook docs) to a running
API, exactly as Meta would, so the API logs show `referral codes: ...` and `conversation ... tagged with ...`
for each one. Uses a throwaway tenant with the AI off (nothing is sent to Meta) and deletes it afterwards.

Usage (on the server, inside the api container):
    docker exec nepsocial-api python -m scripts.mock_referrals            # then: docker logs nepsocial-api
    docker exec nepsocial-api python -m scripts.mock_referrals --keep     # leave the rows to look at
    python -m scripts.mock_referrals --api http://localhost:8005           # from outside the container
"""
import argparse
import asyncio
import hmac
import json
import time
import uuid
from hashlib import sha256

import httpx
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.campaign import CampaignLink
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant

RUN = uuid.uuid4().hex[:6]  # unique ids per run, so it never collides with real accounts or a previous run
IG, PAGE, WA = f"mock-ig-{RUN}", f"mock-page-{RUN}", f"mock-wa-{RUN}"
AD = {"source": "ADS", "type": "OPEN_THREAD",
      "ads_context_data": {"ad_title": "Chinnon sharara", "photo_url": "https://example.com/ad.jpg"}}


def ig_event(customer: str, message: dict | None = None, referral: dict | None = None) -> dict:
    ev = {"sender": {"id": customer}, "recipient": {"id": IG}, "timestamp": int(time.time() * 1000)}
    if message:
        ev["message"] = {"mid": f"mid-{uuid.uuid4().hex}", **message}
    if referral:
        ev["referral"] = referral
    return ev


# (name, what it imitates, path, payload, customer id) — the customer id finds the conversation afterwards.
def scenarios() -> list[tuple[str, str, str, dict, str]]:
    def ig(customer, **kw):
        return {"object": "instagram", "entry": [{"id": IG, "time": 0, "messaging": [ig_event(customer, **kw)]}]}

    return [
        ("ig-plain", "plain DM, no ad or link", "instagram",
         ig(f"c1-{RUN}", message={"text": "hello"}), f"c1-{RUN}"),
        ("ig-ad", "taps an Instagram ad, sends a message (new chat)", "instagram",
         ig(f"c2-{RUN}", message={"text": "price?", "referral": {"ad_id": f"AD-IG-{RUN}", **AD}}), f"c2-{RUN}"),
        ("ig-ad+ref", "Instagram ad that ALSO carries a ref code", "instagram",
         ig(f"c3-{RUN}", message={"text": "pp", "referral": {"ad_id": f"AD-IG2-{RUN}", "ref": "SHARARA-01", **AD}}), f"c3-{RUN}"),
        ("ig-ad-existing", "existing chat taps an ad (referral event, no message)", "instagram",
         {"object": "instagram", "entry": [{"id": IG, "time": 0, "messaging": [  # an earlier DM, then the ad tap
             ig_event(f"c6-{RUN}", message={"text": "hi"}), ig_event(f"c6-{RUN}", referral={"ad_id": f"AD-IG3-{RUN}", **AD})]}]},
         f"c6-{RUN}"),
        ("ig-link", "taps an ig.me?ref= link (referral event, no message)", "instagram",
         ig(f"c4-{RUN}", referral={"ref": f"LINK-{RUN}", "source": "SHORTLINKS", "type": "OPEN_THREAD"}), f"c4-{RUN}"),
        ("fb-ad", "taps a Messenger ad, sends a message", "facebook",
         {"object": "page", "entry": [{"id": PAGE, "time": 0, "messaging": [
             {"sender": {"id": f"c5-{RUN}"}, "recipient": {"id": PAGE}, "timestamp": int(time.time() * 1000),
              "message": {"mid": f"mid-{uuid.uuid4().hex}", "text": "price?",
                          "referral": {"ad_id": f"AD-FB-{RUN}", **AD}}}]}]}, f"c5-{RUN}"),
        ("wa-ad", "taps a click-to-WhatsApp ad, sends a message", "facebook",
         {"object": "whatsapp_business_account", "entry": [{"id": "mock-waba", "changes": [{"field": "messages", "value": {
             "messaging_product": "whatsapp", "metadata": {"phone_number_id": WA},
             "contacts": [{"wa_id": f"977{RUN}", "profile": {"name": "Mock WA"}}],
             "messages": [{"from": f"977{RUN}", "id": f"wamid-{uuid.uuid4().hex}", "timestamp": str(int(time.time())),
                           "type": "text", "text": {"body": "price?"},
                           "referral": {"source_url": "https://fb.me/x", "source_id": f"AD-WA-{RUN}", "source_type": "ad",
                                        "headline": "Chinnon sharara", "body": "Pure chinnon silk, Rs 3350",
                                        "media_type": "image", "image_url": "https://example.com/wa.jpg",
                                        "ctwa_clid": "mock-clid"}}]}}]}]}, f"977{RUN}"),
    ]


def sign(body: bytes) -> str:
    secret = settings.meta_app_secret or settings.instagram_app_secret
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


async def main(api: str, keep: bool) -> None:
    async with async_session_factory() as db:
        tenant = Tenant(name=f"Mock referral test {RUN}", ai_auto_reply=False)  # AI off: nothing goes to Meta
        db.add(tenant)
        await db.flush()
        for platform, ext in (("instagram", IG), ("facebook", PAGE), ("whatsapp", WA)):
            db.add(SocialAccount(tenant_id=tenant.id, platform=platform, external_account_id=ext,
                                 access_token_encrypted=encrypt_token("mock")))
        await db.commit()
        tenant_id = tenant.id

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            for name, _, path, payload, _ in scenarios():
                body = json.dumps(payload).encode()
                r = await http.post(f"{api}/api/v1/webhooks/{path}", content=body,
                                    headers={"content-type": "application/json", "x-hub-signature-256": sign(body)})
                print(f"posted {name:<15} -> HTTP {r.status_code}")

        print(f"\n{'scenario':<15} {'what it imitates':<55} reference code stored on the chat")
        async with async_session_factory() as db:
            for name, what, _, _, customer in scenarios():
                row = (await db.execute(
                    select(CampaignLink.kind, CampaignLink.external_id, CampaignLink.ad_title)
                    .select_from(Conversation)
                    .join(Customer, Customer.id == Conversation.customer_id)
                    .outerjoin(CampaignLink, CampaignLink.id == Conversation.campaign_link_id)
                    .where(Conversation.tenant_id == tenant_id, Customer.external_user_id == customer)
                )).first()
                code = "no chat stored" if row is None else (
                    f"{row.kind} {row.external_id} ({row.ad_title or 'no title'})" if row.kind else "none")
                print(f"{name:<15} {what:<55} {code}")
        print("\nFull detail per event: the api logs, lines 'referral codes:' and 'conversation ... tagged with'.")
    finally:
        if not keep:
            async with async_session_factory() as db:
                # messages first: messages.sender_customer_id has no ON DELETE, so it blocks the customer cascade
                chats = select(Conversation.id).where(Conversation.tenant_id == tenant_id)
                await db.execute(delete(Message).where(Message.conversation_id.in_(chats)))
                await db.execute(delete(Tenant).where(Tenant.id == tenant_id))  # cascades the rest
                await db.commit()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default="http://localhost:8000", help="API base url (default: inside the api container)")
    p.add_argument("--keep", action="store_true", help="keep the mock tenant and its rows")
    a = p.parse_args()
    asyncio.run(main(a.api, a.keep))
