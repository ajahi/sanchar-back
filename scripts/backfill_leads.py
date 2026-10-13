"""Create sales leads from numbers customers already typed in old chats (new chats are captured live).

    python -m scripts.backfill_leads --dry-run     # list what it would create, change nothing
    python -m scripts.backfill_leads               # create/update the leads

Reads every stored customer message oldest-first, so the newest number a customer gave wins, as it does
live. Instagram and Messenger only (WhatsApp customers already have their number). No notifications are
sent for old chats. Safe to re-run: one lead per customer, same result.
"""
import asyncio
import sys

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.sales_lead import SalesLead
from app.models.social_account import SocialAccount
from app.services.leads import capture_lead, extract_phone


async def main(dry_run: bool) -> None:
    async with async_session_factory() as db:
        rows = await db.execute(
            select(Message, Conversation, Customer)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Customer, Customer.id == Conversation.customer_id)
            .where(
                Message.sender_type == "customer",
                Message.content.isnot(None),
                Conversation.channel != "whatsapp",
                Conversation.social_account_id.isnot(None),
            )
            .order_by(Message.created_at)
        )
        accounts: dict = {}
        found: dict = {}  # customer id -> latest number
        for msg, convo, customer in rows.all():
            phone = extract_phone(msg.content)
            if phone is None:
                continue
            found[customer.id] = phone
            if dry_run:
                continue
            if convo.social_account_id not in accounts:
                accounts[convo.social_account_id] = await db.get(SocialAccount, convo.social_account_id)
            await capture_lead(
                db, account=accounts[convo.social_account_id], convo=convo, customer=customer,
                message_id=msg.id, text=msg.content, notify=False,
            )
            await db.flush()  # next message of the same customer must see this lead
        if dry_run:
            print(f"would create/update {len(found)} lead(s)")
        else:
            await db.commit()
            total = len((await db.execute(select(SalesLead.id))).all())
            print(f"{len(found)} customer(s) gave a number; {total} lead(s) in the table now")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" in sys.argv[1:]))
