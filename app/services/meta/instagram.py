"""Instagram API with Instagram Login — OAuth client.

Flow (see Meta docs: Business Login for Instagram):
  1. Send the user to AUTHORIZE_URL.
  2. Exchange the returned `code` for a short-lived token (TOKEN_URL).
  3. Upgrade to a 60-day long-lived token (GRAPH /access_token).
  4. Read the profile (GRAPH /me).
Long-lived tokens are refreshed elsewhere via GRAPH /refresh_access_token.
"""
from urllib.parse import urlencode

import httpx

from app.core.config import settings

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_BASE = "https://graph.instagram.com"

_TIMEOUT = httpx.Timeout(15.0)


def build_authorize_url(state: str) -> str:
    """Step 1 — the URL to redirect the browser to."""
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_redirect_uri,
        "response_type": "code",
        "scope": settings.instagram_scopes,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> dict:
    """Step 2 — code -> short-lived token. Returns {access_token, user_id, permissions?}."""
    data = {
        "client_id": settings.instagram_app_id,
        "client_secret": settings.instagram_app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": settings.instagram_redirect_uri,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(TOKEN_URL, data=data)
        resp.raise_for_status()
        return resp.json()


async def exchange_for_long_lived_token(short_lived_token: str) -> dict:
    """Step 3 — short-lived -> 60-day token. Returns {access_token, token_type, expires_in}."""
    params = {
        "grant_type": "ig_exchange_token",
        "client_secret": settings.instagram_app_secret,
        "access_token": short_lived_token,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/access_token", params=params)
        resp.raise_for_status()
        return resp.json()


async def fetch_profile(access_token: str) -> dict:
    """Step 4 — the connected account's {user_id, username}."""
    params = {"fields": "user_id,username", "access_token": access_token}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/me", params=params)
        resp.raise_for_status()
        return resp.json()


async def refresh_long_lived_token(long_lived_token: str) -> dict:
    """Refresh a 60-day token (must be >=24h old). Returns {access_token, token_type, expires_in}."""
    params = {"grant_type": "ig_refresh_token", "access_token": long_lived_token}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{GRAPH_BASE}/refresh_access_token", params=params)
        resp.raise_for_status()
        return resp.json()
