"""Campaigns: which ad a chat came from, and the price + photos message the bot sends for it.

A chat links to the ad (or `ref` code) it arrived through; staff map each link to a campaign (its own
price, one-line blurb and photos). When the customer then asks for the price / colours / a photo, the bot
answers from the campaign's own fields instead of letting the model phrase a price, then sends the photos.
"""
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign, CampaignLink
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

MAX_PHOTOS = 5
PHOTO_COOLDOWN = timedelta(hours=24)  # one campaign's photos are not resent into the same chat within this
MAX_QUESTION_CHARS = 40  # longer messages ("price of delivery to Pokhara?") go to the AI, not the fixed reply

# Price, colour and photo requests in English, Roman Nepali and Devanagari. "pp" = "price please".
# ponytail: keyword match, not intent classification; swap for the LLM if it misfires on real chats.
_INTENT = re.compile(
    r"(?<![a-z])(pp|price|prices|rate|cost|kati|mulya|colou?rs?|rang|photo|photos|pic|pics|picture|tasbir|dekhau|dekha)(?![a-z])"
    r"|मूल्य|कति|रंग|रङ|फोटो|तस्बिर",
    re.IGNORECASE,
)


_CODE_KEYS = ("ad_id", "ref", "source", "type", "source_id", "source_type", "source_url", "ctwa_clid", "headline", "ads_context_data")


def wants_campaign_info(text: str | None) -> bool:
    """A short message asking for the price, colours or photos."""
    t = (text or "").strip()
    return 0 < len(t) <= MAX_QUESTION_CHARS and _INTENT.search(t) is not None


def referral_of(event: dict) -> dict | None:
    """The ad / ref a message came from, as {kind, external_id, title, image}, or None.

    Instagram / Messenger carry `referral` on the event, on the message, or on a postback; WhatsApp on the
    message itself (source_id = ad id). The exact ad field names are tolerated both ways: ad_id, or
    source_id with a source_type of ad/post.
    """
    r = event.get("referral") or (event.get("message") or {}).get("referral") or (event.get("postback") or {}).get("referral")
    if not isinstance(r, dict):
        return None
    # Every code Meta sent (an ad can carry ad_id AND ref), so a real ad's payload can be read from the logs.
    log.info("referral codes: %s", {k: v for k, v in r.items() if k in _CODE_KEYS and v})
    ctx = r.get("ads_context_data") or {}
    ad_id = r.get("ad_id") or (r.get("source_id") if r.get("source_type") in ("ad", "post") else None)
    if ad_id:
        return {
            "kind": "ad",
            "external_id": str(ad_id)[:255],
            "title": (ctx.get("ad_title") or r.get("headline") or "")[:255] or None,
            "image": ctx.get("photo_url") or ctx.get("video_url") or r.get("image_url") or r.get("thumbnail_url"),
        }
    if r.get("ref"):
        return {"kind": "ref", "external_id": str(r["ref"])[:255], "title": None, "image": None}
    return None


async def apply_referral(db: AsyncSession, account: SocialAccount, convo: Conversation, ref: dict) -> None:
    """Point the chat at the link it came from. An unknown ad / ref registers itself as an unmapped link and
    staff are told once; mapping it to a campaign later applies to every chat that came through it."""
    link = (
        await db.execute(
            select(CampaignLink).where(
                CampaignLink.tenant_id == account.tenant_id,
                CampaignLink.kind == ref["kind"],
                CampaignLink.external_id == ref["external_id"],
            )
        )
    ).scalar_one_or_none()
    if link is None:
        link = CampaignLink(
            tenant_id=account.tenant_id, kind=ref["kind"], external_id=ref["external_id"],
            ad_title=ref["title"], ad_image_url=ref["image"],
        )
        db.add(link)
        await db.flush()
        await create_notification(
            db,
            event="ad_needs_campaign",
            tenant_id=account.tenant_id,
            source_table="campaign_links",
            entity_id=link.id,
            subject=f"New {ref['kind']} needs a campaign: {ref['title'] or ref['external_id']}",
            message="Link it to a campaign so the bot can share its price and photos.",
            payload={"link_id": str(link.id), "kind": ref["kind"], "external_id": ref["external_id"]},
        )
    else:
        link.ad_title = link.ad_title or ref["title"]
        link.ad_image_url = link.ad_image_url or ref["image"]
    convo.campaign_link_id = link.id
    log.info(
        "conversation %s tagged with %s %s (%s)", convo.id, link.kind, link.external_id,
        f"campaign {link.campaign_id}" if link.campaign_id else "not mapped to a campaign yet",
    )


async def campaign_for(db: AsyncSession, convo: Conversation) -> Campaign | None:
    """The active campaign this chat's ad is mapped to, if any."""
    if convo.campaign_link_id is None:
        return None
    return (
        await db.execute(
            select(Campaign)
            .join(CampaignLink, CampaignLink.campaign_id == Campaign.id)
            .where(CampaignLink.id == convo.campaign_link_id, Campaign.status == "active")
        )
    ).scalar_one_or_none()


def campaign_text(c: Campaign) -> str:
    """'Rs 3350 price, pure chinnon silk fabric, comes with quality guarantee'; empty if there is nothing to say."""
    parts = [f"Rs {c.price} price" if c.price else None, (c.blurb or "").strip() or None]
    return ", ".join(p for p in parts if p)


def campaign_context(c: Campaign) -> str:
    """One line for the AI prompt, so other questions in this chat are answered about this item."""
    return f"{c.name}. {campaign_text(c)}".strip()


async def photos_for(db: AsyncSession, c: Campaign) -> list[ShopMedia]:
    """The campaign's in-stock photos in the order staff set, at most MAX_PHOTOS."""
    rows = await db.execute(
        select(ShopMedia)
        .where(ShopMedia.campaign_id == c.id, ShopMedia.tenant_id == c.tenant_id, ShopMedia.in_stock.is_(True))
        .order_by(ShopMedia.sort_order, ShopMedia.created_at)
        .limit(MAX_PHOTOS)
    )
    return list(rows.scalars())


async def photos_sent_recently(db: AsyncSession, conversation_id: uuid.UUID, campaign_id: uuid.UUID) -> bool:
    cutoff = datetime.now(timezone.utc) - PHOTO_COOLDOWN
    found = await db.execute(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_type == "ai",
            Message.message_type == "image",
            Message.created_at > cutoff,
            Message.meta["campaign_id"].astext == str(campaign_id),
        )
        .limit(1)
    )
    return found.first() is not None
