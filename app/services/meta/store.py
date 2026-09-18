"""Shared find-or-create for a messaging participant's Customer + open Conversation.

Used by both the webhook receiver (one event at a time) and the Graph-pull sync
(one thread at a time) so the two ingestion paths can't drift apart.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.social_account import SocialAccount
from app.services.meta import instagram


async def get_or_create_conversation(
    db: AsyncSession, account: SocialAccount, igsid: str, token: str
) -> tuple[Customer, Conversation]:
    # ponytail: select-then-insert; the partial unique indexes catch the rare concurrent
    # first-message race (this delivery fails -> Meta retries -> second attempt finds the rows).
    customer = (
        await db.execute(
            select(Customer).where(
                Customer.tenant_id == account.tenant_id, Customer.external_user_id == igsid
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        profile = await instagram.fetch_customer_profile(token, igsid)
        customer = Customer(
            tenant_id=account.tenant_id,
            external_user_id=igsid,
            name=profile.get("name"),
            external_username=profile.get("username"),
        )
        db.add(customer)
        await db.flush()

    convo = (
        await db.execute(
            select(Conversation).where(
                Conversation.customer_id == customer.id,
                Conversation.social_account_id == account.id,
                Conversation.status == "open",
            )
        )
    ).scalar_one_or_none()
    if convo is None:
        convo = Conversation(
            tenant_id=account.tenant_id,
            customer_id=customer.id,
            social_account_id=account.id,
            channel="instagram",
        )
        db.add(convo)
        await db.flush()
    return customer, convo
