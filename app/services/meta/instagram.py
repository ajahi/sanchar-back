"""Instagram Direct messaging — reading threads, the historical backfill, and replies.

Host-agnostic by design: every call takes a `MetaTarget` (see `target.py`) which carries
the right Graph host, path root and token for the account's auth flow. That is what lets
Instagram Login (graph.instagram.com, Instagram account id) and Facebook Login for
Business (graph.facebook.com, Page id) share this code.

Endpoint shapes:
  GET  /{root}/conversations?platform=instagram   — every IG thread
  GET  /{conversation-id}/messages                — a thread's own messages edge
  POST /{root}/messages                           — send as the business
  GET  /{igsid}?fields=name,username              — customer profile (best effort)
"""
from __future__ import annotations

import hmac
import logging
from hashlib import sha256
from typing import Any, AsyncIterator, Optional

from app.core.config import settings
from app.services.meta import graph
from app.services.meta.target import FACEBOOK_LOGIN, MetaTarget

log = logging.getLogger(__name__)

PLATFORM = "instagram"

# Only fields that provably exist are requested: a nonexistent field makes Graph fail the
# whole call with error #100. The Message node has no `is_echo` (webhook-only) and no
# `is_deleted`, so direction is inferred from `from.id` in the importer instead.
_MESSAGE_FIELDS = "id,message,from,created_time,attachments"

# `messages` is nested inline because that makes a backfill one paginated crawl rather than
# one request per thread — and Meta caps a thread at its 20 most recent messages either way,
# so the separate /messages edge would buy nothing but extra rate-limit pressure.
CONVERSATION_FIELDS = f"id,updated_time,participants,messages{{{_MESSAGE_FIELDS}}}"
MESSAGE_FIELDS = _MESSAGE_FIELDS


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Check Meta's `X-Hub-Signature-256` ("sha256=<hex>") against a known app secret.

    Instagram Login and Facebook Login are separate app identities with separate secrets,
    and which one signs a delivery depends on where the webhook was configured. Both are
    checked rather than guessing — we hold both, so a match against either is authoritative.
    Compared in constant time so a wrong signature cannot be discovered byte by byte.
    """
    if not signature_header:
        return False

    candidate_secrets = [
        s for s in (settings.instagram_app_secret, settings.facebook_app_secret) if s
    ]
    if not candidate_secrets:
        return False

    matched = False
    for secret in candidate_secrets:
        expected = hmac.new(secret.encode(), raw_body, sha256).hexdigest()
        # compare_digest on every candidate: no early exit, so timing reveals nothing.
        matched |= hmac.compare_digest(f"sha256={expected}", signature_header)
    return matched


def attachment_media(attachment: dict) -> tuple[Optional[str], str]:
    """Extract `(url, media_type)` from a webhook *or* a Graph attachment.

    The two shapes differ: webhooks send `{"type": "image", "payload": {"url": ...}}`
    while the Graph API sends `{"name": ..., "file_url": ..., "image_data": {...}}`.
    Reading both keeps a backfilled attachment rendering exactly like a live one.
    """
    if not isinstance(attachment, dict):
        return None, "file"

    payload = attachment.get("payload")
    if isinstance(payload, dict) and payload.get("url"):
        return str(payload["url"]), str(attachment.get("type") or "file")

    for key, kind in (("image_data", "image"), ("video_data", "video")):
        data = attachment.get(key)
        if isinstance(data, dict):
            url = data.get("url") or data.get("preview_url")
            if url:
                return str(url), kind

    if attachment.get("file_url"):
        return str(attachment["file_url"]), str(attachment.get("name") or "file")

    return None, str(attachment.get("type") or attachment.get("name") or "file")


async def iter_conversations(
    target: MetaTarget,
    *,
    page_size: Optional[int] = None,
    fields: str = CONVERSATION_FIELDS,
    max_conversations: Optional[int] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield Instagram conversations, newest first, across all pages.

    Meta returns most-recently-updated first; the importer relies on that ordering to
    decide which thread is a customer's current open conversation.
    """
    params = {
        "platform": PLATFORM,
        "fields": fields,
        "limit": page_size or settings.sync_page_size,
    }
    async for conversation in graph.paginate(
        f"{target.path_root}/conversations",
        token=target.token,
        base_url=target.base_url,
        params=params,
        max_items=max_conversations,
    ):
        yield conversation


async def fetch_conversation(
    conversation_id: str, target: MetaTarget, *, fields: str = CONVERSATION_FIELDS
) -> dict[str, Any]:
    """Read one conversation (used by resync to refresh a single thread)."""
    return await graph.get(
        conversation_id, token=target.token, base_url=target.base_url, fields=fields
    )


async def iter_messages(
    conversation_id: str,
    target: MetaTarget,
    *,
    page_size: Optional[int] = None,
    max_messages: Optional[int] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield a thread's messages via its own edge, for when the inline list is truncated."""
    async for message in graph.paginate(
        f"{conversation_id}/messages",
        token=target.token,
        base_url=target.base_url,
        params={"fields": MESSAGE_FIELDS, "limit": page_size or settings.sync_page_size},
        max_items=max_messages,
    ):
        yield message


async def send_text(target: MetaTarget, recipient_igsid: str, text: str) -> dict[str, Any]:
    """Send a text DM as the business. Returns {recipient_id, message_id}.

    Meta enforces a 24-hour Standard Messaging Window; outside it the call fails with
    error 1545041, which callers translate into something a human can act on.

    `messaging_type` is a Messenger Platform component, so it is only sent on the Facebook
    Login flow. Instagram Login does not document it, and sending an unrecognised field
    risks a rejected request.
    """
    body: dict[str, Any] = {"recipient": {"id": recipient_igsid}, "message": {"text": text}}
    if target.provider == FACEBOOK_LOGIN:
        body["messaging_type"] = "RESPONSE"

    return await graph.request(
        "POST",
        f"{target.path_root}/messages",
        token=target.token,
        base_url=target.base_url,
        json_body=body,
    )


async def fetch_customer_profile(
    igsid: str, target: MetaTarget, *, timeout: Optional[float] = None
) -> dict[str, Any]:
    """Best-effort {name, username} for a messaging participant; {} on any failure.

    Profile lookup needs extra permissions and is frequently denied, so a failure here
    must never break message ingest — the id is enough to keep the thread usable.
    """
    try:
        return await graph.get(
            igsid,
            token=target.token,
            base_url=target.base_url,
            fields="name,username",
            timeout=timeout,
        )
    except graph.GraphError as exc:
        log.debug("profile lookup failed for %s: %s", igsid, exc)
        return {}
