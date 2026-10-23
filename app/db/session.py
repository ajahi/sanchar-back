"""Async SQLAlchemy engine, session factory, and the get_db FastAPI dependency."""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# pool_size connections are kept open; only the overflow beyond them is opened and closed per use, and a new
# asyncpg connection costs a TCP + auth handshake (~8% of CPU in the load-test profile with the old 5 + 10).
# Per process: with N workers keep N * (pool_size + max_overflow) under Postgres max_connections (100).
engine = create_async_engine(
    settings.database_url, pool_pre_ping=True, future=True, pool_size=15, max_overflow=5
)

async_session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session, rolling back on error and always closing."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
