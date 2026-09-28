"""Link the WhatsApp Cloud API number to the tenant that owns the Instagram account, so both
channels land in one inbox.

Usage:  python -m scripts.connect_whatsapp [instagram_username]
Reads WABA_TOKEN (System User token) and WABA_PHONE_NUMBER_ID from .env. The username is only
needed when more than one Instagram account is connected.
Idempotent: re-running refreshes the stored token.
"""
import asyncio
import sys

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.social_account import SocialAccount
from app.services.meta import whatsapp


async def main(ig_username: str | None) -> None:
    token, phone_number_id = settings.waba_token, settings.waba_phone_number_id
    if not (token and phone_number_id):
        sys.exit("Set WABA_TOKEN and WABA_PHONE_NUMBER_ID in .env")

    try:  # fail here, not on the first customer reply, if the token can't see the number
        number = await whatsapp.fetch_phone_number(token, phone_number_id)
    except httpx.HTTPStatusError as exc:
        sys.exit(f"Token can't read phone number {phone_number_id}: {exc.response.text[:300]}")

    async with async_session_factory() as db:
        query = select(SocialAccount).where(SocialAccount.platform == "instagram")
        if ig_username:
            query = query.where(SocialAccount.account_name == ig_username)
        ig = (await db.execute(query)).scalars().all()
        if len(ig) != 1:
            sys.exit(
                f"Found {len(ig)} Instagram accounts {[a.account_name for a in ig]}; "
                "pass the username of the one to link"
            )
        tenant_id = ig[0].tenant_id

        wa = (
            await db.execute(
                select(SocialAccount).where(
                    SocialAccount.platform == "whatsapp",
                    SocialAccount.external_account_id == phone_number_id,
                )
            )
        ).scalar_one_or_none()
        if wa is None:
            wa = SocialAccount(
                tenant_id=tenant_id, platform="whatsapp", external_account_id=phone_number_id
            )
            db.add(wa)
        elif wa.tenant_id != tenant_id:
            sys.exit("This WhatsApp number is already linked to another tenant")

        wa.access_token_encrypted = encrypt_token(token)
        wa.account_name = number.get("verified_name") or number.get("display_phone_number")
        wa.meta = {**(wa.meta or {}), "display_phone_number": number.get("display_phone_number")}
        await db.commit()
        print(f"Linked WhatsApp {number.get('display_phone_number')} to @{ig[0].account_name}'s tenant")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
