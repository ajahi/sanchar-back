"""Validate the Meta configuration and print the exact URLs to register.

Three fields across two Meta screens are all called some flavour of "callback URL":

    OAuth redirect URI   <- where Instagram returns the user AFTER consent
                            Instagram -> API setup with Instagram login -> Business login settings
    Webhook Callback URL <- where Meta POSTs incoming DMs
                            Instagram -> Webhooks -> Callback URL
    Webhook verify token <- the string you make up and paste into BOTH the dashboard and .env

Swapping any two produces browser-side errors that say nothing useful ("Sorry, this page
isn't available", "The callback URL or verify token couldn't be validated"). This script
checks the configuration statically and prints the values to paste where, so the mistake is
caught in a terminal instead of a browser.

Usage:
    python -m scripts.check_meta              # static checks only
    python -m scripts.check_meta --live       # also probe each stored account token
"""
import argparse
import asyncio
import sys
from urllib.parse import urlparse

from app.core.config import settings
from app.services.meta import instagram_login as ig_login

OK, WARN, ERROR = "ok", "warn", "error"

_MARK = {OK: "  ok  ", WARN: " warn ", ERROR: "ERROR "}

# The OAuth callback path for each flow, as registered on the route table.
IG_CALLBACK_PATH = "/api/v1/social-accounts/instagram/callback"
FB_CALLBACK_PATH = "/api/v1/social-accounts/facebook/callback"
WEBHOOK_PATH = "/api/v1/webhooks/instagram"


def _looks_like_secret(value: str) -> bool:
    """A Meta app secret is 32 hex characters; an app id is numeric."""
    cleaned = value.strip().lower()
    return len(cleaned) == 32 and all(c in "0123456789abcdef" for c in cleaned)


def _check_app_pair(label: str, app_id: str, secret: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    prefix = label.upper()

    if not app_id:
        out.append((ERROR, f"{prefix}_APP_ID is empty"))
    elif _looks_like_secret(app_id):
        out.append(
            (
                ERROR,
                f"{prefix}_APP_ID={app_id!r} is 32 hex characters — that is an app SECRET, "
                "not an app id. App ids are numeric. You have swapped them.",
            )
        )
    elif not app_id.isdigit():
        out.append((WARN, f"{prefix}_APP_ID={app_id!r} is not numeric; app ids normally are"))

    if not secret:
        out.append((ERROR, f"{prefix}_APP_SECRET is empty"))
    elif secret == app_id:
        out.append((ERROR, f"{prefix}_APP_ID and {prefix}_APP_SECRET are the same value"))

    return out


def _check_redirect(label: str, uri: str, expected_path: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    prefix = label.upper()

    if not uri:
        out.append((ERROR, f"{prefix}_REDIRECT_URI is empty"))
        return out

    parsed = urlparse(uri)
    path = parsed.path

    if not parsed.scheme or not parsed.netloc:
        out.append((ERROR, f"{prefix}_REDIRECT_URI={uri!r} is not an absolute URL"))
        return out
    # The single most common mix-up: the webhook endpoint is not an OAuth redirect URI.
    if path.rstrip("/") == WEBHOOK_PATH:
        out.append(
            (
                ERROR,
                f"{prefix}_REDIRECT_URI points at the WEBHOOK endpoint ({path}). That field "
                f"wants the OAuth callback: {expected_path}",
            )
        )
    elif not path.rstrip("/").endswith(expected_path):
        out.append(
            (
                WARN,
                f"{prefix}_REDIRECT_URI path is {path!r}; expected it to end with "
                f"{expected_path}",
            )
        )
    if parsed.scheme != "https":
        out.append((WARN, f"{prefix}_REDIRECT_URI is not https; Meta requires https in production"))

    return out


def _check_webhook() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    token = settings.meta_webhook_verify_token
    if not token:
        out.append(
            (
                ERROR,
                "META_WEBHOOK_VERIFY_TOKEN is empty — Meta's handshake will get a 403 and no "
                "webhooks will ever be delivered.",
            )
        )
    elif len(token) < 16:
        out.append(
            (
                WARN,
                f"META_WEBHOOK_VERIFY_TOKEN is only {len(token)} characters. It must merely "
                "match what you paste into the dashboard, but a short one is guessable.",
            )
        )
    return out


def _report(title: str, results: list[tuple[str, str]]) -> int:
    print(f"\n{title}")
    errors = 0
    for level, message in results:
        print(f"  [{_MARK[level]}] {message}")
        if level == ERROR:
            errors += 1
    if not results:
        print("  [  ok  ] no issues")
    return errors


async def _live_checks() -> int:
    """Probe each stored account token against its own provider's host."""
    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.social_account import SocialAccount
    from app.services.meta import graph
    from app.services.meta.target import resolve_target

    print("\nlive checks")
    async with async_session_factory() as db:
        accounts = (
            await db.execute(select(SocialAccount).order_by(SocialAccount.created_at))
        ).scalars().all()

        if not accounts:
            print("  [ warn ] no connected accounts to probe")
            return 0

        failures = 0
        for account in accounts:
            label = account.account_name or account.external_account_id
            try:
                target = resolve_target(account)
            except ValueError as exc:
                print(f"  [ERROR ] {label}: {exc}")
                failures += 1
                continue
            try:
                profile = await graph.get(
                    "me",
                    token=target.token,
                    base_url=target.base_url,
                    fields="user_id,username",
                )
                print(
                    f"  [  ok  ] {label} ({target.provider}): token valid, "
                    f"profile={profile.get('username') or profile.get('user_id')}"
                )
            except graph.GraphError as exc:
                print(f"  [ERROR ] {label} ({target.provider}): {exc}")
                failures += 1
        return failures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="also probe stored tokens")
    args = parser.parse_args()

    print("=" * 78)
    print("Meta configuration check")
    print("=" * 78)

    errors = 0

    ig_on = settings.instagram_login_configured
    print(f"\nInstagram Login (primary): {'configured' if ig_on else 'NOT configured'}")
    errors += _report(
        "  credentials",
        _check_app_pair("instagram", settings.instagram_app_id, settings.instagram_app_secret)
        + _check_redirect(
            "instagram", settings.instagram_redirect_uri, IG_CALLBACK_PATH
        ),
    )

    fb_on = settings.facebook_login_configured
    print(f"\nFacebook Login (secondary): {'configured' if fb_on else 'not configured (fine)'}")
    if fb_on:
        errors += _report(
            "  credentials",
            _check_app_pair(
                "facebook", settings.facebook_app_id, settings.facebook_app_secret
            )
            + _check_redirect(
                "facebook", settings.facebook_redirect_uri, FB_CALLBACK_PATH
            ),
        )

    errors += _report("Webhooks", _check_webhook())

    if settings.instagram_app_id and settings.facebook_app_id:
        if settings.instagram_app_id == settings.facebook_app_id:
            print(
                "\n  [ERROR ] INSTAGRAM_APP_ID and FACEBOOK_APP_ID are the same value. They "
                "are different app identities."
            )
            errors += 1

    # The values to paste into the dashboard, printed where they cannot be misread.
    base = settings.frontend_url.rstrip("/")
    print("\n" + "-" * 78)
    print("Paste these into the Meta dashboard")
    print("-" * 78)
    host = urlparse(settings.instagram_redirect_uri).netloc or "<your-host>"
    print(f"  Instagram -> Business login settings -> OAuth Redirect URI:")
    print(f"      https://{host}{IG_CALLBACK_PATH}")
    print(f"  Instagram -> Webhooks -> Callback URL:")
    print(f"      https://{host}{WEBHOOK_PATH}")
    print(f"  Instagram -> Webhooks -> Verify token:")
    print(f"      {settings.meta_webhook_verify_token or '<not set>'}")
    print(f"\n  Authorize URL your browser will be sent to (check client_id is numeric):")
    print(f"      {ig_login.build_authorize_url('dry-run')}")

    if args.live:
        errors += await _live_checks()

    print("\n" + "=" * 78)
    if errors:
        print(f"RESULT: {errors} error(s) — fix these before opening a browser.")
    else:
        print("RESULT: configuration looks valid.")
    print("=" * 78)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
