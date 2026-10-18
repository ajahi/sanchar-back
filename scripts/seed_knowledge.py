"""Give a tenant knowledge and switch its AI auto-reply on (there is no UI for either yet).

    python -m scripts.seed_knowledge owner@example.com            # sample shop knowledge, AI on
    python -m scripts.seed_knowledge owner@example.com --off      # AI back off, knowledge kept
    python -m scripts.seed_knowledge owner@example.com --clear    # remove the sample shop document (AI off too)
    python -m scripts.seed_knowledge owner@example.com my.txt     # your own knowledge text file

Idempotent: re-running replaces the "Store information" document.
"""
import asyncio
import sys

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.knowledge import KnowledgeDocument
from app.models.tenant import Tenant
from app.models.user import User
from scripts.cag_demo import KNOWLEDGE

TITLE = "Store information"


async def main(email: str, off: bool, path: str | None, clear: bool = False) -> None:
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None or user.tenant_id is None:
            sys.exit(f"No tenant user with email {email}")
        tenant = await db.get(Tenant, user.tenant_id)
        tenant.ai_auto_reply = not (off or clear)
        if clear:
            for doc in (
                await db.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == tenant.id, KnowledgeDocument.title == TITLE
                    )
                )
            ).scalars():
                await db.delete(doc)
        elif not off:
            text = open(path, encoding="utf-8").read() if path else KNOWLEDGE
            doc = (
                await db.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == tenant.id, KnowledgeDocument.title == TITLE
                    )
                )
            ).scalar_one_or_none()
            if doc is None:
                db.add(KnowledgeDocument(tenant_id=tenant.id, title=TITLE, document_type="store_info", content=text))
            else:
                doc.content, doc.status = text, "active"
        await db.commit()
        print(f"{tenant.name}: AI auto-reply {'OFF' if off or clear else 'ON'}{' (sample document removed)' if clear else ''}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    asyncio.run(main(args[0], "--off" in sys.argv, args[1] if len(args) > 1 else None, "--clear" in sys.argv))
