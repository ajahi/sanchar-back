import os

import pytest

# Tests re-create accounts under the same external id, so a cached lookup would be stale (set before app imports).
os.environ.setdefault("ACCOUNT_CACHE_SECONDS", "0")

from app.db.session import engine


@pytest.fixture(autouse=True)
def _fresh_pool():
    """Each test runs its own asyncio.run(); drop pooled connections bound to the dead loop."""
    yield
    engine.sync_engine.dispose(close=False)
