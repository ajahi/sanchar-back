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
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
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
from app.models.order_integration import OrderIntegration
from app.models.sales_lead import SalesLead
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.models.funnel import FunnelSubmission
from app.services.auto_reply import (
    build_system_prompt, expand_picks, generate_reply, photo_hint, sent_photos_note, split_long, split_photos,
)
from app.services import campaigns, funnels, hours, kitchen
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
    """In-stock photos an admin described, newest first: the list the model may pick photos from. An uploaded
    photo counts once it has a name or tags; an imported Instagram post only once it is tagged, because its
    automatic name is just the caption's first line ("Blue me away.") and most posts are not products.
    ponytail: newest MAX_PROMPT_PHOTOS only; a bigger catalogue needs a pre-filter on the customer's words."""
    rows = await db.execute(
        select(ShopMedia)
        .where(
            ShopMedia.tenant_id == tenant_id,
            ShopMedia.in_stock.is_(True),
            or_(
                func.cardinality(ShopMedia.tags) > 0,
                and_(ShopMedia.source == "upload", ShopMedia.title.isnot(None)),
            ),
        )
        .order_by(ShopMedia.created_at.desc())
        .limit(MAX_PROMPT_PHOTOS)
    )
    return list(rows.scalars())


async def _generate(system: str, history: list[tuple[str, str]], text: str, hint: str = "") -> str:
    """One retry: the API occasionally answers 400/5xx/timeouts, and the customer is waiting."""
    try:
        return await generate_reply(system, history, text, hint)
    except httpx.HTTPError:
        return await generate_reply(system, history, text, hint)


async def _send_as_ai(
    db: AsyncSession,
    convo: Conversation,
    send: Callable[[], Awaitable[dict]],
    *,
    message_type: str = "text",
    content: str | None = None,
    media_url: str | None = None,
    meta: dict | None = None,
) -> None:
    """Store the bot's message, then send it, then give the row Meta's message id.

    Instagram / Messenger echo every message the page sends back as a webhook, often BEFORE the send call
    returns (always for a photo: Meta fetches it first). The row is committed before sending so that echo finds
    it (webhooks.claim_own_echo) instead of being stored as a person replying, which would pause the bot.
    If the echo was stored anyway (it beat the commit, or did not match), that row is relabelled as ours.
    For a photo, meta.campaign_id lets campaigns skip resending its photos for a while, and content
    ("[sent photo: name]") puts it in the model's history. Raises httpx.HTTPError if the send fails (row removed)."""
    row = Message(
        conversation_id=convo.id, sender_type="ai", message_type=message_type, content=content,
        media_url=media_url, ai_generated=True, meta=meta or {},
    )
    db.add(row)
    await db.commit()  # visible to the echo's transaction; also releases the connection for the Graph call
    try:
        sent = await send()
    except httpx.HTTPError:
        await db.delete(row)
        await db.commit()
        raise
    mid = sent.get("message_id")
    if mid:
        try:
            async with db.begin_nested():  # fails if the echo was stored under this id: adopted just below
                await db.execute(
                    update(Message)
                    .where(Message.id == row.id, Message.external_message_id.is_(None))
                    .values(external_message_id=mid)
                )
        except IntegrityError:
            pass
        echo = (
            await db.execute(
                update(Message)
                .where(Message.external_message_id == mid, Message.id != row.id)
                .values(sender_type="ai", ai_generated=True)
                .returning(Message.id)
            )
        ).first()
        if echo:
            await db.delete(row)
    convo.last_message_at = datetime.now(timezone.utc)
    await db.commit()


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
    try:
        await _send_as_ai(
            db, convo, lambda: channel.send_text(token, acct, to, text), content=text, meta={"model": settings.llm_model}
        )
    except httpx.HTTPError as exc:
        log.exception("sending campaign reply failed for conversation %s: %s", convo.id, _meta_error(exc))
        return
    send_image = getattr(channel, "send_image", None)
    for media in photos if send_image else []:
        try:
            await _send_as_ai(
                db, convo, lambda: send_image(token, acct, to, media.url),
                message_type="image", media_url=media.url, meta={"campaign_id": str(campaign.id)},
            )
        except httpx.HTTPError as exc:
            log.warning("sending campaign photo %s failed for conversation %s: %s", media.id, convo.id, _meta_error(exc))
            break  # the rest would most likely fail the same way (expired url, window closed)


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
    token, acct, to = decrypt_token(account.access_token_encrypted), account.external_account_id, customer.external_user_id
    try:
        await _send_as_ai(
            db, convo, lambda: send_text(token, acct, to, PHOTO_HOLDING_REPLY),
            content=PHOTO_HOLDING_REPLY, meta={"model": settings.llm_model},
        )
    except httpx.HTTPError as exc:
        log.exception("sending photo holding reply failed for conversation %s: %s", convo.id, _meta_error(exc))


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
    funnel = await funnels.for_tenant(db, tenant)
    closed = await funnels.closed_message(db, tenant, funnel, knowledge)  # may re-read the hours: saved by the commit below
    areas = await delivery_areas(db, tenant.id)

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
    listed = [(p.title, p.tags) for p in photos]
    try:
        reply = await _generate(
            build_system_prompt(knowledge, campaigns.campaign_context(campaign) if campaign else "", listed,
                                funnels.prompt_block(funnel)),
            history,
            msg.content,
            _hint(msg.content, listed, closed, areas),
        )
    except Exception:  # noqa: BLE001 — stay quiet rather than send something wrong; a person can answer
        log.exception("LLM failed for conversation %s", convo.id)
        return
    reply, collected = await funnels.finish_reply(db, tenant.id, funnel, reply or "", closed)
    reply, picks = split_photos(reply, len(photos))
    kitchen_job = None
    if collected:  # kept even if the reply is dropped below: the customer did give (and confirm) these details
        sub = await funnels.record(db, funnel=funnel, account=account, convo=convo, customer=customer, fields=collected)
        log.info("%s confirmed in conversation %s (tenant %s, funnel %s v%s): %s", funnel.label, convo.id, tenant.id,
                 funnel.key, funnel.version, "saved" if sub else "same as just before, not saved again")
        if sub is not None:
            await db.commit()  # kept whatever happens to the website send
            if "website_post" in funnel.triggers and sub.lead_id:  # into the business's own order system
                kitchen_job = (sub.lead_id, sub.id)
    try:
        await _send_reply(db, convo, account, customer, msg, reply, picks, photos, listed)
    finally:  # after the reply: the customer isn't kept waiting on the order system (5 s and more)
        if kitchen_job:
            await _send_to_kitchen(*kitchen_job)


async def _send_reply(db, convo, account, customer, msg, reply: str, picks: list[int], photos, listed) -> None:
    """The reply text (split if long), then the photos the model picked."""
    if not reply and not picks:
        return

    # A person usually jumps in DURING the second or two of generation: re-check right before sending.
    if await human_active(db, convo.id):
        log.info("auto-reply dropped: a person took over conversation %s while generating", convo.id)
        return
    channel = CHANNELS[account.platform]
    token, acct, to = decrypt_token(account.access_token_encrypted), account.external_account_id, customer.external_user_id
    try:  # the model may answer with only a photo label (no text); a long answer goes as several messages
        for part in split_long(reply):
            await _send_as_ai(
                db, convo, lambda part=part: channel.send_text(token, acct, to, part), content=part,
                meta={"model": settings.llm_model},
            )
    except httpx.HTTPError as exc:
        log.exception("sending AI reply failed for conversation %s: %s", convo.id, _meta_error(exc))
        return

    # The photos the model picked (each grown into its set, e.g. every page of a menu), after the text: if one
    # fails the customer still has the answer. Only the first row carries the history note: the history holds
    # the last HISTORY rows with content, and ten photo rows would push the conversation out of it.
    send_image = getattr(channel, "send_image", None)
    chosen = [photos[i] for i in expand_picks(msg.content, listed, picks)] if send_image else []
    note = sent_photos_note([m.title or "photo" for m in chosen])
    for n, media in enumerate(chosen):
        try:
            await _send_as_ai(
                db, convo, lambda: send_image(token, acct, to, media.url), message_type="image", media_url=media.url,
                content=None if n else note, meta={"media_id": str(media.id)},
            )
        except httpx.HTTPError as exc:
            log.warning("sending photo %s failed for conversation %s: %s", media.id, convo.id, _meta_error(exc))
            break  # the rest would most likely fail the same way (expired url, window closed)


async def delivery_areas(db: AsyncSession, tenant_id) -> list[dict]:
    """The delivery areas synced from the business's own order system ([] without one)."""
    return (
        await db.execute(select(OrderIntegration.locations).where(OrderIntegration.tenant_id == tenant_id))
    ).scalar() or []


def _hint(text: str, listed: list[tuple[str | None, list[str]]], closed: str | None, areas: list[dict]) -> str:
    """The per-message note for the model: matching photos, the delivery area named, and that the business is closed."""
    notes = [photo_hint(text, listed), kitchen.area_hint(text, areas), hours.closed_note(closed) if closed else ""]
    return "\n\n".join(x for x in notes if x)


async def preview_reply(
    db: AsyncSession, tenant: Tenant, message: str, history: list[tuple[str, str]] | None = None, funnel=None
) -> str | None:
    """What the bot would answer `message` after `history` right now, with the photos it would attach and what it
    would save written out after it. Nothing is sent or stored. funnel: try one other than the tenant's.
    None when the tenant has no shop context. Raises httpx.HTTPError when the model is down."""
    knowledge = await build_knowledge(db, tenant.id)
    if not knowledge:
        return None
    photos = await sendable_photos(db, tenant.id)
    funnel = funnel or await funnels.for_tenant(db, tenant)
    closed = await funnels.closed_message(db, tenant, funnel, knowledge)
    areas = await delivery_areas(db, tenant.id)
    listed = [(p.title, p.tags) for p in photos]
    reply = await generate_reply(
        build_system_prompt(knowledge, photos=listed, funnel=funnels.prompt_block(funnel)),
        history or [], message, _hint(message, listed, closed, areas),
    )
    reply, collected = await funnels.finish_reply(db, tenant.id, funnel, reply, closed)
    reply, picks = split_photos(reply, len(photos))
    shown = [f"[would send photo: {photos[i].title or 'untitled'}]" for i in expand_picks(message, listed, picks)]
    if collected:
        shown.append(f"[would save {funnel.label}: " + " | ".join(collected.values()) + "]")
    return "\n".join(x for x in [reply, *shown] if x)


async def _send_to_kitchen(lead_id, submission_id) -> None:
    """kitchen.auto_send in its own session: whatever fails there (the order system down, a table missing after
    a deploy) never costs the customer their reply or the saved order. The outcome goes on the submission."""
    try:
        async with async_session_factory() as s:
            lead = await s.get(SalesLead, lead_id)
            if lead is not None:
                outcome = await kitchen.auto_send(s, lead)
                sub = await s.get(FunnelSubmission, submission_id)
                if sub is not None and outcome != "off":
                    sub.status = outcome
                    sub.result = {k: lead.meta[k] for k in ("kitchen", "kitchen_error") if k in (lead.meta or {})}
                await s.commit()
    except Exception:  # noqa: BLE001
        log_failure(log, "kitchen send failed for lead %s", lead_id)


async def run_auto_reply(message_id) -> None:
    """Background-task entry: own session, own commit, never raises into the request."""
    try:
        async with async_session_factory() as db:
            await auto_reply(db, message_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        log_failure(log, "auto-reply failed for message %s", message_id)
