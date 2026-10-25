"""Auto-reply pipeline: one stored customer message in -> (maybe) one AI reply out.

The webhook stores the message and commits, then queues run_auto_reply(message_id) as a background
task, so Meta gets its 200 right away.

Human takeover is a timer, as in the Messenger MVP: while a person has replied in this chat within
the last HUMAN_PAUSE_MINUTES (from the Instagram app via an echo, or from the dashboard), the bot
stays quiet; every human reply refreshes the window, and once they go idle the bot takes over again.
"Human replied recently" is read from the stored messages (sender_type = 'agent'), so there is no
mode to flip back and nothing to lose on a restart.

ponytail: FastAPI BackgroundTasks, so a server restart loses in-flight replies; move to a jobs table
+ worker if that ever matters.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.log import log_failure
from app.core.security import decrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.handover import HandoverEvent
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services.auto_reply import build_system_prompt, generate_reply, split_photos
from app.services import campaigns
from app.services.meta import CHANNELS
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

HISTORY = 6  # previous messages given to the model
MAX_PROMPT_PHOTOS = 40  # shop photos listed to the model (~20 tokens each)
PHOTO_REASON = "customer_sent_image"
PHOTO_HOLDING_REPLY = (
    "Tapai ko photo pauna payo. Hamro team le herera chadai reply garnu hunchha. "
    "(Thanks for the photo, our team will check it and reply shortly.)"
)


def _meta_error(exc: httpx.HTTPError) -> str:
    """Meta's own error text for a failed send (the bare 400 says nothing about why)."""
    return exc.response.text[:500] if isinstance(exc, httpx.HTTPStatusError) else repr(exc)


async def human_active(db: AsyncSession, conversation_id) -> bool:
    """True while a person has replied in this chat within the pause window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.human_pause_minutes)
    found = await db.execute(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_type == "agent",
            Message.created_at > cutoff,
        )
        .limit(1)
    )
    return found.first() is not None


async def build_knowledge(db: AsyncSession, tenant_id) -> str:
    """The tenant's whole shop context as one text block (the CAG context). Tenant-scoped, always."""
    docs = (
        await db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.status == "active")
            .order_by(KnowledgeDocument.created_at)
        )
    ).scalars()
    text = "\n\n".join(f"## {d.title}\n{d.content.strip()}" for d in docs if d.content and d.content.strip())
    if not text:
        return text
    # Items the admin marked sold out in the media panel; the Products text alone can't know.
    sold_out = (
        await db.execute(
            select(ShopMedia.title).where(
                ShopMedia.tenant_id == tenant_id, ShopMedia.in_stock.is_(False), ShopMedia.title.isnot(None)
            )
        )
    ).scalars().all()
    if sold_out:
        text += "\n\nSOLD OUT right now (do not offer these): " + "; ".join(sold_out)
    return text


async def sendable_photos(db: AsyncSession, tenant_id) -> list[ShopMedia]:
    """In-stock photos with a name or tags, newest first: the list the model may pick photos from.
    ponytail: newest MAX_PROMPT_PHOTOS only; a bigger catalogue needs a pre-filter on the customer's words."""
    rows = await db.execute(
        select(ShopMedia)
        .where(
            ShopMedia.tenant_id == tenant_id,
            ShopMedia.in_stock.is_(True),
            or_(ShopMedia.title.isnot(None), func.cardinality(ShopMedia.tags) > 0),
        )
        .order_by(ShopMedia.created_at.desc())
        .limit(MAX_PROMPT_PHOTOS)
    )
    return list(rows.scalars())


async def _generate(system: str, history: list[tuple[str, str]], text: str) -> str:
    """One retry: the API occasionally answers 400/5xx/timeouts, and the customer is waiting."""
    try:
        return await generate_reply(system, history, text)
    except httpx.HTTPError:
        return await generate_reply(system, history, text)


async def _store_ai_message(db: AsyncSession, convo: Conversation, sent: dict, text: str) -> None:
    """Stored under Meta's message id so our own echo (Instagram sends one back) hits the unique index
    and is not mistaken for a person replying. If the echo won the race, relabel its row as ours."""
    stmt = pg_insert(Message).values(
        conversation_id=convo.id,
        external_message_id=sent.get("message_id"),
        sender_type="ai",
        message_type="text",
        content=text,
        ai_generated=True,
        meta={"model": settings.llm_model},
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
            set_={"sender_type": "ai", "ai_generated": True},
        )
    )
    convo.last_message_at = datetime.now(timezone.utc)


async def _store_ai_image(
    db: AsyncSession, convo: Conversation, sent: dict, url: str, meta: dict, content: str | None = None
) -> None:
    """Like _store_ai_message, for a photo we sent. meta.campaign_id lets us skip resending a campaign's photos
    for a while; content ("[sent photo: name]") puts the photo in the model's history, so it knows it showed it."""
    stmt = pg_insert(Message).values(
        conversation_id=convo.id,
        external_message_id=sent.get("message_id"),
        sender_type="ai",
        message_type="image",
        content=content,
        media_url=url,
        ai_generated=True,
        meta=meta,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["external_message_id"],
            index_where=Message.external_message_id.isnot(None),
            set_={"sender_type": "ai", "ai_generated": True},
        )
    )
    convo.last_message_at = datetime.now(timezone.utc)


async def share_campaign(db: AsyncSession, convo, campaign, account, customer) -> None:
    """The ad's price line, then its in-stock photos (not again within the cooldown). The text goes first and
    stays even if a photo fails: the customer already has the price."""
    channel = CHANNELS[account.platform]
    text = campaigns.campaign_text(campaign)
    photos = (
        []
        if await campaigns.photos_sent_recently(db, convo.id, campaign.id)
        else await campaigns.photos_for(db, campaign)
    )
    token, acct, to = decrypt_token(account.access_token_encrypted), account.external_account_id, customer.external_user_id
    await db.commit()  # release the connection for the Graph calls
    try:
        sent = await channel.send_text(token, acct, to, text)
    except httpx.HTTPError as exc:
        log.exception("sending campaign reply failed for conversation %s: %s", convo.id, _meta_error(exc))
        return
    await _store_ai_message(db, convo, sent, text)
    await db.commit()
    send_image = getattr(channel, "send_image", None)
    for media in photos if send_image else []:
        try:
            sent = await send_image(token, acct, to, media.url)
        except httpx.HTTPError as exc:
            log.warning("sending campaign photo %s failed for conversation %s: %s", media.id, convo.id, _meta_error(exc))
            break  # the rest would most likely fail the same way (expired url, window closed)
        await _store_ai_image(db, convo, sent, media.url, {"campaign_id": str(campaign.id)})
        await db.commit()


async def hand_over_photo(db: AsyncSession, convo, tenant, account, customer) -> None:
    """A customer's photo (usually "do you have this?") is for a person: the bot does not guess from
    pixels. Record the handover, ping the dashboard, tell the customer someone will look.
    ponytail: no mode flip, the reply timer takes over once a person answers; a sticky mode if photos
    must keep the bot silent until someone clicks 'hand back'."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.human_pause_minutes)
    recent = await db.execute(
        select(HandoverEvent.id)
        .where(
            HandoverEvent.conversation_id == convo.id,
            HandoverEvent.reason == PHOTO_REASON,
            HandoverEvent.created_at > cutoff,
        )
        .limit(1)
    )
    if recent.first() is not None:  # several photos in a row: one handover, one holding reply
        return
    db.add(
        HandoverEvent(
            conversation_id=convo.id, from_mode="ai", to_mode="human",
            reason=PHOTO_REASON, triggered_by="system", notes="Customer sent an image",
        )
    )
    who = customer.name or customer.external_username or "a customer"
    await create_notification(
        db,
        event="handover_created",
        tenant_id=tenant.id,
        source_table="conversations",
        entity_id=convo.id,
        subject=f"{who} sent a photo",
        message="A customer sent an image. The bot did not answer; please reply.",
        payload={"conversation_id": str(convo.id), "customer_id": str(customer.id), "reason": PHOTO_REASON},
    )
    # The holding reply is the bot talking: only when AI replies are on and the channel can send.
    if not tenant.ai_auto_reply or account is None or not account.access_token_encrypted:
        return
    await db.commit()  # keep the handover even if sending the holding reply blows up below
    send_text = CHANNELS[account.platform].send_text
    try:
        sent = await send_text(
            decrypt_token(account.access_token_encrypted),
            account.external_account_id,
            customer.external_user_id,
            PHOTO_HOLDING_REPLY,
        )
    except httpx.HTTPError as exc:
        log.exception("sending photo holding reply failed for conversation %s: %s", convo.id, _meta_error(exc))
        return
    await _store_ai_message(db, convo, sent, PHOTO_HOLDING_REPLY)


async def auto_reply(db: AsyncSession, message_id) -> None:
    # One joined query, not five db.get round-trips (each costs ~0.5ms of CPU, and this runs per message).
    row = (
        await db.execute(
            select(Message, Conversation, Tenant, SocialAccount, Customer)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Tenant, Tenant.id == Conversation.tenant_id)
            .outerjoin(SocialAccount, SocialAccount.id == Conversation.social_account_id)
            .outerjoin(Customer, Customer.id == Conversation.customer_id)
            .where(Message.id == message_id)
        )
    ).first()
    if row is None:
        return
    msg, convo, tenant, account, customer = row
    if msg.sender_type != "customer":
        return
    is_photo = msg.message_type == "image"
    if not is_photo and not (msg.content or "").strip():
        return

    # A photo is flagged for a person even when AI replies are off (nobody else will mark it).
    if is_photo:
        if customer is None or await human_active(db, convo.id):
            return
        await hand_over_photo(db, convo, tenant, account, customer)
        return

    if not tenant.ai_auto_reply:
        log.info("auto-reply skipped: AI is off for tenant %s", tenant.id)
        return
    if await human_active(db, convo.id):
        log.info("auto-reply skipped: a person is handling conversation %s", convo.id)
        return
    if account is None or not account.access_token_encrypted or customer is None:
        log.info("auto-reply skipped: conversation %s has no connected account", convo.id)
        return
    # A price / colour / photo question in a chat that came from a mapped ad gets that campaign's own
    # message and photos, no model involved (works before the shop has written any knowledge).
    campaign = await campaigns.campaign_for(db, convo)
    if campaign and campaigns.campaign_text(campaign) and campaigns.wants_campaign_info(msg.content):
        await share_campaign(db, convo, campaign, account, customer)
        return
    knowledge = await build_knowledge(db, tenant.id)
    if not knowledge:
        log.info("auto-reply skipped: tenant %s has no shop context yet", tenant.id)
        return
    photos = await sendable_photos(db, tenant.id)

    prior = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == convo.id,
                Message.id != msg.id,
                Message.created_at <= msg.created_at,
                Message.content.isnot(None),
            )
            .order_by(Message.created_at.desc())
            .limit(HISTORY)
        )
    ).scalars()
    history = [("customer" if m.sender_type == "customer" else "assistant", m.content) for m in reversed(list(prior))]

    # Everything needed is in memory now. Commit hands the DB connection back to the pool (the loaded
    # rows stay usable: expire_on_commit=False) so the 1-3s LLM call below does not hold one.
    await db.commit()
    try:
        reply = await _generate(
            build_system_prompt(
                knowledge,
                campaigns.campaign_context(campaign) if campaign else "",
                [(p.title, p.tags) for p in photos],
            ),
            history,
            msg.content,
        )
    except Exception:  # noqa: BLE001 — stay quiet rather than send something wrong; a person can answer
        log.exception("LLM failed for conversation %s", convo.id)
        return
    reply, picks = split_photos(reply or "", len(photos))
    if not reply and not picks:
        return

    # A person usually jumps in DURING the second or two of generation: re-check right before sending.
    if await human_active(db, convo.id):
        log.info("auto-reply dropped: a person took over conversation %s while generating", convo.id)
        return
    await db.commit()  # release the connection again for the Graph send; _store_ai_message takes a fresh one

    channel = CHANNELS[account.platform]
    token, acct, to = decrypt_token(account.access_token_encrypted), account.external_account_id, customer.external_user_id
    if reply:  # the model may answer with only a photo label
        try:
            sent = await channel.send_text(token, acct, to, reply)
        except httpx.HTTPError as exc:
            log.exception("sending AI reply failed for conversation %s: %s", convo.id, _meta_error(exc))
            return
        await _store_ai_message(db, convo, sent, reply)

    # The photos the model picked, after the text: if one fails the customer still has the answer.
    send_image = getattr(channel, "send_image", None)
    for media in [photos[i] for i in picks] if send_image else []:
        await db.commit()  # keep what is sent so far, and release the connection for the Graph call
        try:
            sent = await send_image(token, acct, to, media.url)
        except httpx.HTTPError as exc:
            log.warning("sending photo %s failed for conversation %s: %s", media.id, convo.id, _meta_error(exc))
            break  # the rest would most likely fail the same way (expired url, window closed)
        await _store_ai_image(
            db, convo, sent, media.url, {"media_id": str(media.id)}, f"[sent photo: {media.title or 'photo'}]"
        )


async def run_auto_reply(message_id) -> None:
    """Background-task entry: own session, own commit, never raises into the request."""
    try:
        async with async_session_factory() as db:
            await auto_reply(db, message_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        log_failure(log, "auto-reply failed for message %s", message_id)
