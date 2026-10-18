"""Create N throwaway tenants for the load test, each with an Instagram account, AI replies on and
shop knowledge (without knowledge the bot stays quiet, which would hide the LLM path).

    python -m loadtest.seed_tenants 20          # idempotent; writes loadtest/accounts.json for k6
    python -m loadtest.seed_tenants --cleanup   # deletes every tenant named loadtest-*

Run it against a throwaway database (override DATABASE_URL), not your dev data.
"""
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import delete, select

from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.knowledge import KnowledgeDocument
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant

PREFIX = "loadtest-"
KNOWLEDGE = "Shop: Load Test Gifts, Kathmandu.\nProducts: Simple Plain Chura Rs 300 per set, in stock.\nDelivery: inside Kathmandu in 1-2 days.\nContact: 9800000000"


async def seed(n: int) -> None:
    ids = []
    async with async_session_factory() as db:
        for i in range(n):
            name, ig_id = f"{PREFIX}{i:03d}", f"lt_ig_{i:03d}"
            ids.append(ig_id)
            if (await db.execute(select(Tenant.id).where(Tenant.name == name))).first():
                continue
            t = Tenant(name=name, business_type="loadtest", ai_auto_reply=True)
            db.add(t)
            await db.flush()
            db.add_all(
                [
                    SocialAccount(
                        tenant_id=t.id, platform="instagram", external_account_id=ig_id,
                        account_name=name, access_token_encrypted=encrypt_token("mock-token"),
                    ),
                    KnowledgeDocument(tenant_id=t.id, title="Shop", content=KNOWLEDGE),
                ]
            )
        await db.commit()
    Path(__file__).with_name("accounts.json").write_text(json.dumps({"accounts": ids}))
    print(f"{n} tenants ready; accounts.json written (first one is the 'hot' boosted shop)")


async def cleanup() -> None:
    async with async_session_factory() as db:
        res = await db.execute(delete(Tenant).where(Tenant.name.like(f"{PREFIX}%")))
        await db.commit()
        print(f"deleted {res.rowcount} tenants (cascade removes their data)")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "20"
    asyncio.run(cleanup() if arg == "--cleanup" else seed(int(arg)))
