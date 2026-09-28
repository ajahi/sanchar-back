"""WhatsApp ingest: customer keyed by wa_id with the contact's name, one whatsapp conversation,
message idempotent on wamid, and an unknown phone_number_id is ignored.

Runs against the configured Postgres inside a transaction that is always rolled back.
    python -m pytest tests/test_wa_ingest.py
"""
import asyncio

from sqlalchemy import select

from app.api.v1.webhooks import ingest_whatsapp_message
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant


async def _run() -> None:
    async with async_session_factory() as db:
        t = Tenant(name="T")
        db.add(t)
        await db.flush()
        db.add(
            SocialAccount(
                tenant_id=t.id,
                platform="whatsapp",
                external_account_id="pn-1",
                access_token_encrypted=encrypt_token("tok"),
            )
        )
        await db.flush()

        contacts = [{"profile": {"name": "Ram"}, "wa_id": "9779800000000"}]
        text = {"from": "9779800000000", "id": "wamid.1", "timestamp": "1700000000",
                "type": "text", "text": {"body": "hi"}}
        image = {"from": "9779800000000", "id": "wamid.2", "timestamp": "1700000001",
                 "type": "image", "image": {"id": "media-1", "caption": "this one?"}}
        for msg in (text, text, image):  # second `text` = Meta redelivery
            await ingest_whatsapp_message(db, "pn-1", contacts, msg)
        await ingest_whatsapp_message(db, "pn-unknown", contacts, {**text, "id": "wamid.3"})

        customer = (await db.execute(select(Customer).where(Customer.tenant_id == t.id))).scalar_one()
        assert (customer.name, customer.phone, customer.external_user_id) == ("Ram", "+9779800000000", "9779800000000")
        convo = (await db.execute(select(Conversation).where(Conversation.tenant_id == t.id))).scalar_one()
        assert convo.channel == "whatsapp"
        msgs = (
            await db.execute(
                select(Message).where(Message.conversation_id == convo.id).order_by(Message.external_message_id)
            )
        ).scalars().all()
        assert [(m.external_message_id, m.message_type, m.content) for m in msgs] == [
            ("wamid.1", "text", "hi"),
            ("wamid.2", "image", "this one?"),
        ]
        await db.rollback()


def test_wa_ingest() -> None:
    asyncio.run(_run())
