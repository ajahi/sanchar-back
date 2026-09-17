"""Instagram Login — the primary connect flow ("Instagram API with Instagram Login").

This is the Instagram-branded consent screen, and it is what the dashboard's
"Continue with Instagram" button is expected to do. It is distinct from
`facebook_login.py` in three ways that matter:

* **Different app identity.** `client_id` is the *Instagram* app id from
  Instagram -> API setup with Instagram login, not the Facebook app id from
  App Settings -> Basic. Mixing them produces
  "Invalid app ID: The provided app ID does not look like a valid app ID".
* **Different host.** Tokens are used against `graph.instagram.com`, never
  `graph.facebook.com` — an Instagram token is rejected outright there with code 190.
* **Tokens expire.** The long-lived token lasts ~60 days and must be refreshed
  (`refresh_long_lived_token`), unlike a Page token.

Flow:
  1. Redirect to the authorize dialog                    -> `build_authorize_url`
  2. `code` -> short-lived token (1 hour)                -> `exchange_code_for_token`
  3. short-lived -> long-lived token (~60 days)          -> `exchange_for_long_lived_token`
  4. Read the profile (id + username)                    -> `fetch_profile`
  5. Subscribe the account to messaging webhooks         -> `subscribe_webhooks`
  6. Refresh before expiry (token must be >=24h old)     -> `refresh_long_lived_token`
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlencode

from app.core.config import settings
from app.services.meta import graph

log = logging.getLogger(__name__)

_AUTHORIZE_PATH = "oauth/authorize"
_TOKEN_PATH = "oauth/access_token"
_ACCESS_TOKEN_PATH = "access_token"
_REFRESH_PATH = "refresh_access_token"

# Keeping the account's own id as the path root is what the conversations/messages edges
# expect on graph.instagram.com, so no Page is involved anywhere in this flow.
PROFILE_FIELDS = "user_id,username"


def scopes() -> list[str]:
    """Configured Instagram permission list, split and de-blanked."""
    return [s.strip() for s in settings.instagram_scopes.split(",") if s.strip()]


def configured() -> bool:
    return settings.instagram_login_configured


def build_authorize_url(state: str) -> str:
    """Step 1 — the URL that shows Instagram's own consent screen."""
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes()),
        "state": state,
    }
    version = settings.graph_api_version.strip("/")
    base = settings.instagram_dialog_url.rstrip("/")
    return f"{base}/{version}/{_AUTHORIZE_PATH}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict[str, Any]:
    """Step 2 — authorization code -> short-lived token (about an hour).

    Posted as a form body: `api.instagram.com/oauth/access_token` rejects a JSON body.
    Returns `{access_token, user_id, permissions}`.
    """
    data = {
        "client_id": settings.instagram_app_id,
        "client_secret": settings.instagram_app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": settings.instagram_redirect_uri,
        "code": code,
    }
    return await graph.request(
        "POST", _TOKEN_PATH, base_url=settings.instagram_oauth_base_url, data=data
    )


async def exchange_for_long_lived_token(short_lived_token: str) -> dict[str, Any]:
    """Step 3 — short-lived -> ~60-day token. Returns {access_token, expires_in}."""
    return await graph.get(
        _ACCESS_TOKEN_PATH,
        base_url=settings.instagram_graph_base_url,
        grant_type="ig_exchange_token",
        client_secret=settings.instagram_app_secret,
        access_token=short_lived_token,
    )


async def refresh_long_lived_token(long_lived_token: str) -> dict[str, Any]:
    """Step 6 — extend a long-lived token by another ~60 days.

    Instagram only allows this once the token is at least 24 hours old, and refuses it
    after expiry, so it must run on a schedule well before `token_expires_at`.
    """
    return await graph.get(
        _REFRESH_PATH,
        base_url=settings.instagram_graph_base_url,
        grant_type="ig_refresh_token",
        access_token=long_lived_token,
    )


async def fetch_profile(long_lived_token: str) -> dict[str, Any]:
    """Step 4 — the connected account's id and username.

    Instagram Login returns `user_id`; some responses use `id`. Callers should accept
    either, which `account_id_from_profile` does.
    """
    return await graph.get(
        "me",
        token=long_lived_token,
        base_url=settings.instagram_graph_base_url,
        fields=PROFILE_FIELDS,
    )


def account_id_from_profile(profile: dict[str, Any]) -> str:
    """The Instagram account id, whichever key this response used.

    `user_id` is what Instagram Login documents; `id` shows up in practice. Preferring
    `id` would be wrong here because `/me` on this host has historically returned both.
    """
    for key in ("user_id", "id"):
        value = profile.get(key)
        if value:
            return str(value)
    return ""


async def subscribe_webhooks(long_lived_token: str) -> dict[str, Any]:
    """Step 5 — opt this Instagram account into messaging webhooks.

    Instagram Login subscribes per account via `/me/subscribed_apps`. `messages` is the
    field that also carries the business's own outbound replies as `is_echo: true`.
    """
    return await graph.request(
        "POST",
        "me/subscribed_apps",
        token=long_lived_token,
        base_url=settings.instagram_graph_base_url,
        params={"subscribed_fields": "messages"},
    )


async def list_webhook_subscriptions(long_lived_token: str) -> dict[str, Any]:
    """Current per-account subscriptions (diagnostics)."""
    return await graph.get(
        "me/subscribed_apps",
        token=long_lived_token,
        base_url=settings.instagram_graph_base_url,
    )


async def debug_token(input_token: str) -> dict[str, Any]:
    """Inspect a token's validity, scopes and expiry (diagnostics).

    Instagram Login has no app access token, so the token debugs itself.
    """
    payload = await graph.get(
        "debug_token",
        token=input_token,
        base_url=settings.instagram_graph_base_url,
        input_token=input_token,
    )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def expires_in_seconds(payload: dict[str, Any], default: int = 60 * 24 * 3600) -> Optional[int]:
    """Token lifetime from a token response, tolerating a missing/garbled value."""
    raw = payload.get("expires_in")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
