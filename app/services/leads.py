"""Sales leads: spot a phone number in a customer's message, save it on the customer and as a lead.

Runs when the webhook stores a message (not in the AI pipeline), so a lead is captured whether or
not the bot is switched on, and whoever the number was given to (the bot or a person).
"""
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.sales_lead import SalesLead
from app.models.social_account import SocialAccount
from app.services.notifications import create_notification

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# Nepali mobile: 97/98 + 8 digits, optionally behind +977 / 977 / 00977, with spaces or dashes inside.
# The lookbehind keeps it from starting in the middle of a longer number (an order id, a card).
# ponytail: mobiles only; landlines (01-xxxxxxx) and other countries are not picked up.
_PHONE = re.compile(r"(?<!\d)(?:(?:\+|00)?977[\s-]?)?(9[78](?:[\s-]?\d){8})(?!\d)")


def extract_phone(text: str | None) -> str | None:
    """The first Nepali mobile number in `text` as +977XXXXXXXXXX, or None."""
    m = _PHONE.search((text or "").translate(_DEVANAGARI_DIGITS))
    return "+977" + re.sub(r"\D", "", m.group(1)) if m else None


async def capture_lead(
    db: AsyncSession,
    *,
    account: SocialAccount,
    convo: Conversation,
    customer: Customer,
    message_id: uuid.UUID,
    text: str | None,
) -> bool:
    """If `text` carries a phone number: save it on the customer, upsert the sales lead (one per
    customer; a newer number replaces the old one) and notify staff the first time. Adds, never commits."""
    phone = extract_phone(text)
    if phone is None:
        return False
    customer.phone = phone
    lead = (
        await db.execute(
            select(SalesLead).where(SalesLead.tenant_id == account.tenant_id, SalesLead.customer_id == customer.id)
        )
    ).scalar_one_or_none()
    if lead is None:
        db.add(
            SalesLead(
                tenant_id=account.tenant_id,
                customer_id=customer.id,
                conversation_id=convo.id,
                social_account_id=account.id,
                source_message_id=message_id,
                channel=account.platform,
                phone=phone,
            )
        )
        who = customer.name or customer.external_username or "A customer"
        await create_notification(
            db,
            event="lead_captured",
            tenant_id=account.tenant_id,
            source_table="conversations",
            entity_id=convo.id,
            subject=f"{who} shared their number",
            message=phone,
            payload={"conversation_id": str(convo.id), "customer_id": str(customer.id), "phone": phone},
        )
    else:
        lead.phone, lead.conversation_id, lead.source_message_id = phone, convo.id, message_id
    return True
