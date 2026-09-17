"""Facebook Login for Business — connect a Page and the Instagram identity it owns.

Why this flow rather than "Instagram API with Instagram Login": the product is a single
inbox for Meta messaging. Facebook Login for Business hands back a **Page** token, and a
Page token is what drives both Messenger and the Instagram conversations/messages edges
(`/{page-id}/conversations?platform=instagram`). One consent screen therefore covers
Instagram today and Messenger/WhatsApp later, instead of one auth model per channel.

Flow:
  1. Redirect the browser to the OAuth dialog          -> `build_authorize_url`
  2. Exchange `code` for a short-lived user token      -> `exchange_code_for_user_token`
  3. Upgrade that to a ~60-day long-lived user token   -> `exchange_for_long_lived_user_token`
  4. Read the user's Pages, their Page tokens and the
     Instagram professional account each Page owns     -> `fetch_pages`
  5. Subscribe the Page to messaging webhooks          -> `subscribe_page_webhooks`

Page tokens minted from a long-lived user token do not expire, which is why
`social_accounts.token_expires_at` stays NULL for accounts connected this way.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlencode

from app.core.config import settings
from app.services.meta import graph

log = logging.getLogger(__name__)

# The consent dialog lives on www.facebook.com; everything else is a Graph call.
_DIALOG_PATH = "dialog/oauth"

# Webhook fields, kept per-channel because Meta validates the field names against the
# subscribed object. Instagram messaging documents exactly this set — notably it has no
# `message_echoes`: the business's own outbound replies arrive as a normal `messages`
# event carrying `is_echo: true`, which the ingest path already understands.
INSTAGRAM_SUBSCRIBED_FIELDS = (
    "messages",
    "messaging_postbacks",
    "message_reactions",
    "messaging_seen",
    "messaging_referral",
    "standby",
)

# Messenger-only fields; only valid when subscribing the `page` object.
MESSENGER_SUBSCRIBED_FIELDS = (
    "messages",
    "messaging_postbacks",
    "message_reactions",
    "message_echoes",
    "messaging_handovers",
    "messaging_optins",
    "messaging_referrals",
    "message_deliveries",
)

# Kept as the default so existing callers keep working; Instagram is the current channel.
MESSAGING_SUBSCRIBED_FIELDS = INSTAGRAM_SUBSCRIBED_FIELDS

# Page fields needed to register an account and its linked Instagram identity.
_PAGE_FIELDS = "id,name,access_token,tasks,instagram_business_account{id,username}"


def scopes() -> list[str]:
    """Configured permission list, split and de-blanked."""
    return [s.strip() for s in settings.meta_scopes.split(",") if s.strip()]


def app_access_token() -> str:
    """`{app-id}|{app-secret}` — used for app-level calls like /debug_token."""
    return f"{settings.meta_app_id}|{settings.meta_app_secret}"


def configured() -> bool:
    return settings.meta_configured


def build_authorize_url(state: str) -> str:
    """Step 1 — the URL to bounce the browser to."""
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes()),
        "state": state,
    }
    version = settings.graph_api_version.strip("/")
    base = settings.graph_oauth_dialog_url.rstrip("/")
    return f"{base}/{version}/{_DIALOG_PATH}?{urlencode(params)}"


async def exchange_code_for_user_token(code: str) -> dict[str, Any]:
    """Step 2 — authorization code -> short-lived user token (valid ~1-2 hours)."""
    return await graph.get(
        "oauth/access_token",
        client_id=settings.meta_app_id,
        client_secret=settings.meta_app_secret,
        redirect_uri=settings.meta_redirect_uri,
        code=code,
    )


async def exchange_for_long_lived_user_token(short_lived_token: str) -> dict[str, Any]:
    """Step 3 — short-lived -> long-lived user token (~60 days)."""
    return await graph.get(
        "oauth/access_token",
        grant_type="fb_exchange_token",
        client_id=settings.meta_app_id,
        client_secret=settings.meta_app_secret,
        fb_exchange_token=short_lived_token,
    )


async def fetch_pages(user_token: str) -> list[dict[str, Any]]:
    """Step 4 — every Page the user manages, with its Page token and linked IG account.

    Pages the user granted no access to simply will not appear; a Page with no
    `instagram_business_account` is Messenger-only and is returned as-is so the caller
    can decide whether it is worth storing.
    """
    pages: list[dict[str, Any]] = []
    async for page in graph.paginate(
        "me/accounts", token=user_token, params={"fields": _PAGE_FIELDS}
    ):
        pages.append(page)
    return pages


async def fetch_page_instagram_account(
    page_id: str, page_token: str
) -> Optional[dict[str, Any]]:
    """Re-read one Page's linked Instagram professional account (used by resync/repair)."""
    payload = await graph.get(
        page_id, token=page_token, fields="instagram_business_account{id,username}"
    )
    ig = payload.get("instagram_business_account")
    return ig if isinstance(ig, dict) and ig.get("id") else None


async def subscribe_page_webhooks(
    page_id: str,
    page_token: str,
    *,
    fields: tuple[str, ...] = INSTAGRAM_SUBSCRIBED_FIELDS,
) -> dict[str, Any]:
    """Step 5 — opt this Page into messaging webhooks.

    Per-Page subscription; without it Meta delivers nothing for this Page even when the
    app-level webhook is configured.
    """
    return await graph.request(
        "POST",
        f"{page_id}/subscribed_apps",
        token=page_token,
        params={"subscribed_fields": ",".join(fields)},
    )


async def list_page_subscriptions(page_id: str, page_token: str) -> dict[str, Any]:
    """Which app subscriptions are currently active on this Page (diagnostics)."""
    return await graph.get(f"{page_id}/subscribed_apps", token=page_token)


async def subscribe_app_webhooks(
    callback_url: str,
    verify_token: str,
    *,
    object_: str = "instagram",
    fields: tuple[str, ...] = INSTAGRAM_SUBSCRIBED_FIELDS,
) -> dict[str, Any]:
    """Register the app-level webhook that Meta delivers events to.

    Normally a one-off click in the App Dashboard, but doing it through the API keeps the
    callback URL and verify token in code so they cannot silently drift from config.
    `object_` must match what the channel actually sends: Instagram messaging arrives as
    `object: "instagram"`, Messenger as `object: "page"`.
    """
    return await graph.request(
        "POST",
        f"{settings.meta_app_id}/subscriptions",
        token=app_access_token(),
        params={
            "object": object_,
            "callback_url": callback_url,
            "verify_token": verify_token,
            "fields": ",".join(fields),
            "include_values": "true",
        },
    )


async def list_app_webhooks() -> dict[str, Any]:
    """Current app-level webhook subscriptions (diagnostics)."""
    return await graph.get(f"{settings.meta_app_id}/subscriptions", token=app_access_token())


async def debug_token(input_token: str) -> dict[str, Any]:
    """Inspect a token: type, app, scopes, expiry.

    This is the supported way to answer "is this token still good, and what may it do?",
    which beats discovering an expired token from a failed customer reply.
    """
    payload = await graph.get(
        "debug_token", input_token=input_token, access_token=app_access_token()
    )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}
