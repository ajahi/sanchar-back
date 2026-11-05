import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

# Tests re-create accounts under the same external id, so a cached lookup would be stale (set before app imports).
os.environ.setdefault("ACCOUNT_CACHE_SECONDS", "0")
os.environ.setdefault("KNOWLEDGE_CACHE_SECONDS", "0")

from app.core.config import settings  # noqa: E402

# Tests never touch the database in DATABASE_URL: they use <name>_test, re-created before every test as a copy of
# <name>_test_template (migrated once per run), so no test sees another's rows. The role needs CREATEDB.
_url = make_url(settings.database_url)
DB, TEMPLATE = f"{_url.database}_test", f"{_url.database}_test_template"
settings.database_url = _url.set(database=DB).render_as_string(hide_password=False)

from app.db.session import engine  # noqa: E402  (built from the test URL set just above)


async def _admin(*sql: str) -> None:
    conn = await asyncpg.connect(_url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False))
    try:
        for statement in sql:
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _template():
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{TEMPLATE}" WITH (FORCE)', f'CREATE DATABASE "{TEMPLATE}"'))
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], check=True, cwd=Path(__file__).parents[1],
        env={**os.environ, "DATABASE_URL": _url.set(database=TEMPLATE).render_as_string(hide_password=False)},
    )


@pytest.fixture(autouse=True)
def _fresh_db(_template):
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)', f'CREATE DATABASE "{DB}" TEMPLATE "{TEMPLATE}"'))
    yield
    # Each test runs its own asyncio.run(); drop pooled connections bound to the dead loop.
    engine.sync_engine.dispose(close=False)
