"""Which Graph host, path root and token to use for a connected account.

The two Meta auth flows address completely different hosts and identifiers:

| provider          | host                    | path root      | token            |
|-------------------|-------------------------|----------------|------------------|
| `instagram_login` | graph.instagram.com     | IG account id  | IG long-lived    |
| `facebook_login`  | graph.facebook.com      | Page id        | Page (no expiry) |

Downstream code should not have to know that, so it asks for a `MetaTarget` and uses
`target.base_url`, `target.path_root` and `target.token` directly. This is the single place
that mapping lives, which is what makes it safe to support both flows at once.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.config import settings
from app.core.security import decrypt_token
from app.models.social_account import SocialAccount

INSTAGRAM_LOGIN = "instagram_login"
FACEBOOK_LOGIN = "facebook_login"

PROVIDERS = (INSTAGRAM_LOGIN, FACEBOOK_LOGIN)


@dataclass(frozen=True)
class MetaTarget:
    """Everything a Graph call needs, resolved for one account."""

    base_url: str
    # Goes in `/{path_root}/conversations` and `/{path_root}/messages`.
    path_root: str
    token: str
    provider: str


def provider_of(account: SocialAccount) -> str:
    """The account's auth flow, defaulting to Instagram Login.

    Existing rows predate the column and were all created by the Instagram Login flow, so
    that is the correct default rather than a guess.
    """
    return account.auth_provider or INSTAGRAM_LOGIN


def resolve_target(account: SocialAccount) -> MetaTarget:
    """Build the target for an account. Raises ValueError if it has no usable token."""
    if not account.access_token_encrypted:
        raise ValueError(f"account {account.id} has no stored access token")

    provider = provider_of(account)
    if provider == FACEBOOK_LOGIN:
        # Facebook Login reaches Instagram *through* the Page, so the Page id is the root.
        path_root = account.external_page_id or account.external_account_id
        base_url = settings.facebook_graph_base_url
    else:
        # Instagram Login addresses the Instagram professional account directly.
        path_root = account.external_account_id
        base_url = settings.instagram_graph_base_url

    return MetaTarget(
        base_url=base_url,
        path_root=path_root,
        token=decrypt_token(account.access_token_encrypted),
        provider=provider,
    )


def describe(account: SocialAccount) -> Optional[str]:
    """Short human-readable summary for logs and CLIs."""
    try:
        target = resolve_target(account)
    except ValueError:
        return None
    return f"{target.provider} via {target.base_url} as {target.path_root}"
