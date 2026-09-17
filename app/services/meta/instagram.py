"""Instagram Direct messaging, driven by a Facebook Page access token.

This is the channel client: reading threads (including the historical backfill), sending
replies, and verifying inbound webhook signatures. All HTTP goes through
`app.services.meta.graph`, so retries, throttling and Meta error decoding are shared.

Endpoint shapes (Page-token / Facebook Login for Business):
  GET  /{page-id}/conversations?platform=instagram   — every IG thread on the Page
  GET  /{conversation-id}?fields=messages{...}       — messages within one thread
  POST /{page-id}/messages                           — send as the business
  GET  /{igsid}?fields=name,username                 — customer profile (best effort)
"""
from __future__ import annotations

import hmac
import logging
from hashlib import sha256
from typing import Any, AsyncIterator, Optional

from app.core.config import settings
from app.services.meta import graph

log = logging.getLogger(__name__)

PLATFORM = "instagram"

# Fields for the conversation list. `messages` is nested inline because that is what makes
# a backfill a single paginated crawl instead of one request per thread — and because Meta
# caps a thread at its 20 most recent messages either way, so the separate /messages edge
# would buy nothing but extra rate-limit pressure.
#
# Only fields that provably exist are requested: a nonexistent field makes Graph fail the
# whole call with error #100. The Message node has no `is_echo` (webhook-only) and no
# `is_deleted`, so direction is inferred from `from.id` in the importer instead.
_MESSAGE_FIELDS = "id,message,from,created_time,attachments"
CONVERSATION_FIELDS = f"id,updated_time,participants,messages{{{_MESSAGE_FIELDS}}}"

# Message fields for walking a single thread's own edge.
MESSAGE_FIELDS = _MESSAGE_FIELDS


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


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Check Meta's `X-Hub-Signature-256` ("sha256=<hex>") against the app secret.

    Compared in constant time so a wrong signature cannot be discovered byte by byte.
    """
    if not signature_header or not settings.meta_app_secret:
        return False
    expected = hmac.new(
        settings.meta_app_secret.encode(), raw_body, sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


async def iter_conversations(
    page_id: str,
    token: str,
    *,
    page_size: Optional[int] = None,
    fields: str = CONVERSATION_FIELDS,
    max_conversations: Optional[int] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield Instagram conversations for a Page, newest first, across all pages.

    Meta returns most-recently-updated first; the importer relies on that ordering to
    decide which thread is a customer's current open conversation.
    """
    params = {
        "platform": PLATFORM,
        "fields": fields,
        "limit": page_size or settings.sync_page_size,
    }
    async for conversation in graph.paginate(
        f"{page_id}/conversations",
        token=token,
        params=params,
        max_items=max_conversations,
    ):
        yield conversation


async def fetch_conversation(
    conversation_id: str, token: str, *, fields: str = CONVERSATION_FIELDS
) -> dict[str, Any]:
    """Read one conversation (used by resync to refresh a single thread)."""
    return await graph.get(conversation_id, token=token, fields=fields)


async def iter_messages(
    conversation_id: str,
    token: str,
    *,
    page_size: Optional[int] = None,
    max_messages: Optional[int] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield a thread's messages via its own edge, for when the inline list is truncated."""
    async for message in graph.paginate(
        f"{conversation_id}/messages",
        token=token,
        params={"fields": MESSAGE_FIELDS, "limit": page_size or settings.sync_page_size},
        max_items=max_messages,
    ):
        yield message


async def send_text(
    page_id: str, token: str, recipient_igsid: str, text: str
) -> dict[str, Any]:
    """Send a text DM as the business. Returns {recipient_id, message_id}.

    `messaging_type: RESPONSE` is a documented required component of the send payload.
    Meta enforces a 24-hour Standard Messaging Window; outside it the call fails with
    error 1545041, which callers translate into something a human can act on.
    """
    body = {
        "recipient": {"id": recipient_igsid},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }
    return await graph.request("POST", f"{page_id}/messages", token=token, json_body=body)


async def fetch_customer_profile(
    igsid: str, token: str, *, timeout: Optional[float] = None
) -> dict[str, Any]:
    """Best-effort {name, username} for a messaging participant; {} on any failure.

    Profile lookup needs extra permissions and is frequently denied, so a failure here
    must never break message ingest — the id is enough to keep the thread usable.
    """
    try:
        return await graph.get(
            igsid, token=token, fields="name,username", timeout=timeout
        )
    except graph.GraphError as exc:
        log.debug("profile lookup failed for %s: %s", igsid, exc)
        return {}
