"""Customer profile enrichment.

Looking a participant up (`GET /{igsid}?fields=name,username`) is a permission-gated
network call. Doing it inside the webhook handler would hold Meta's delivery open for as
long as the Graph API takes, and Meta retries — and eventually disables — endpoints that
answer slowly.

So ingest stores the message immediately and hands the affected customers here. This runs
after the response has been sent, on its own DB session, and is allowed to fail quietly:
a missing display name is cosmetic, a lost message is not.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.customer import Customer
from app.models.social_account import SocialAccount
from app.services.meta import instagram
from app.services.meta.target import resolve_target

log = logging.getLogger(__name__)

# Short budget: this is background polish, not something worth retrying hard for.
_ENRICH_TIMEOUT_SECONDS = 5.0


async def enrich_customers(pending: Iterable[tuple[str, str]]) -> int:
    """Fill in name/username for (igsid, social_account_id) pairs. Returns rows updated.

    Never raises: enrichment runs detached from any request, so an exception here would
    only surface as an unhandled background-task error.
    """
    # The same customer can appear several times in one delivery; look each up once.
    unique: list[tuple[str, str]] = list(dict.fromkeys(pending))
    if not unique:
        return 0

    updated = 0
    async with async_session_factory() as db:
        for igsid, account_id in unique:
            try:
                account = await db.get(SocialAccount, uuid.UUID(account_id))
                if account is None:
                    continue
                # The profile lookup must hit the same host that issued the token.
                target = resolve_target(account)
                profile = await instagram.fetch_customer_profile(
                    igsid, target, timeout=_ENRICH_TIMEOUT_SECONDS
                )
                if not profile:
                    continue

                customer = (
                    await db.execute(
                        select(Customer).where(
                            Customer.tenant_id == account.tenant_id,
                            Customer.external_user_id == igsid,
                        )
                    )
                ).scalar_one_or_none()
                if customer is None:
                    continue

                changed = False
                if profile.get("name") and customer.name != profile["name"]:
                    customer.name = profile["name"]
                    changed = True
                if profile.get("username") and customer.external_username != profile["username"]:
                    customer.external_username = profile["username"]
                    changed = True
                if changed:
                    await db.commit()
                    updated += 1
            except Exception:  # noqa: BLE001 — background best-effort
                await db.rollback()
                log.warning("profile enrichment failed for %s", igsid, exc_info=True)

    return updated
