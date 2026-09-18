"""Pull-based backfill from Meta.

The webhook (app/api/v1/webhooks.py) only ever sees messages sent *after* it's
subscribed — it can't backfill history that already existed on Instagram when a
tenant connects. This walks the Conversations API (/me/conversations, then
per-message detail lookups) and upserts into the same conversations/messages
tables the webhook writes to, so both paths feed one inbox.

Meta only serves details for the 20 most recent messages per thread (older ones
404 as "deleted"), so MESSAGE_CAP is a hard ceiling, not a tuning knob.
"""
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_token
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.services.meta import instagram
from app.services.meta.store import get_or_create_conversation

MESSAGE_CAP = 20


def _igsid(msg: dict, own_ig_id: str) -> str | None:
    """The customer's id from a message's {from, to} — whichever side isn't us."""
    sender = msg.get("from", {}).get("id")
    if sender and sender != own_ig_id:
        return sender
    to = msg.get("to", {})
    recipients = to.get("data", [to]) if isinstance(to, dict) else []
    for r in recipients:
        if r.get("id") and r["id"] != own_ig_id:
            return r["id"]
    return None


async def sync_account(db: AsyncSession, account: SocialAccount) -> int:
    """Upsert this account's threads + recent messages. Returns the number of threads synced."""
    if not account.access_token_encrypted:
        return 0
    token = decrypt_token(account.access_token_encrypted)

    synced = 0
    after = None
    while True:
        page = await instagram.list_conversations(token, after=after)
        for thread in page.get("data", []):
            stubs = (await instagram.fetch_conversation_message_stubs(token, thread["id"]))[:MESSAGE_CAP]
            if not stubs:
                continue

            details = [await instagram.fetch_message_detail(token, s["id"]) for s in stubs]
            igsids = (_igsid(m, account.external_account_id) for m in details)
            igsid = next((i for i in igsids if i), None)
            if igsid is None:
                continue

            customer, convo = await get_or_create_conversation(db, account, igsid, token)
            convo.external_conversation_id = thread["id"]

            latest_at = None
            for msg in details:
                is_echo = msg.get("from", {}).get("id") == account.external_account_id
                created = datetime.fromisoformat(msg["created_time"])
                latest_at = created if latest_at is None else max(latest_at, created)
                await db.execute(
                    pg_insert(Message)
                    .values(
                        conversation_id=convo.id,
                        external_message_id=msg["id"],
                        sender_type="agent" if is_echo else "customer",
                        sender_customer_id=None if is_echo else customer.id,
                        content=msg.get("message"),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["external_message_id"],
                        index_where=Message.external_message_id.isnot(None),
                    )
                )
            if latest_at and (convo.last_message_at is None or latest_at > convo.last_message_at):
                convo.last_message_at = latest_at
            synced += 1

        paging = page.get("paging", {})
        after = paging.get("cursors", {}).get("after") if paging.get("next") else None
        if not after:
            break

    await db.commit()
    return synced
