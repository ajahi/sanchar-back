import pytest

from app.db.session import engine


@pytest.fixture(autouse=True)
def _fresh_pool():
    """Each test runs its own asyncio.run(); drop pooled connections bound to the dead loop."""
    yield
    engine.sync_engine.dispose(close=False)
