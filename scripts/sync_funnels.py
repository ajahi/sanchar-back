"""Make each funnels/*.md the live version of its funnel. Runs on every deploy (deploy.yml), so a pushed .md is live.

    python -m scripts.sync_funnels

A file that matches the live version changes nothing. Otherwise it is checked the way the super admin's Load .md and
Save check it (the same endpoint code) and saved as a new live version; older versions stay for a rollback. A file
that fails the check stops the deploy, and nothing of it is saved.
The repo is the source: a version saved in the super admin is replaced on the next deploy if its .md file differs.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.admin import FunnelFile, FunnelIn, funnel_from_md, save_funnel
from app.db.session import async_session_factory
from app.models.funnel import Funnel

FOLDER = Path(__file__).resolve().parent.parent / "funnels"
DEPLOY = SimpleNamespace(id=None)  # the audit row's actor: no person, the deploy


def _same(body: FunnelIn, live: Funnel | None) -> bool:
    return live is not None and (live.name, live.label, live.steps, live.fields, live.triggers, live.checks) == (
        body.name, body.label, body.steps, [f.model_dump() for f in body.fields], body.triggers, body.checks)


async def main() -> None:
    failed = False
    for path in sorted(FOLDER.glob("*.md")):
        async with async_session_factory() as db:
            try:
                body = FunnelIn.model_validate(await funnel_from_md(FunnelFile(markdown=path.read_text("utf-8"))))
                live = await db.scalar(select(Funnel).where(Funnel.key == body.key, Funnel.active.is_(True)))
                if _same(body, live):
                    print(f"{path.name}: {body.key} v{live.version} is live, unchanged")
                    continue
                saved = await save_funnel(body, DEPLOY, db)
                print(f"{path.name}: {body.key} v{saved['version']} saved and live")
            except HTTPException as exc:
                print(f"{path.name}: NOT saved: {exc.detail}", file=sys.stderr)
                failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
