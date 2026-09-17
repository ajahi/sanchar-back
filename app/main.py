"""FastAPI application entrypoint for the NepSocial backend."""
import logging
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import api_router
from app.core.config import settings
from app.db.session import get_db

logging.basicConfig(
    level=(settings.log_level or "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

app = FastAPI(
    title="NepSocial Backend",
    version="0.1.0",
    # The interactive docs are a development affordance. In production they hand the
    # entire API surface to anyone who finds the host, so they are switched off.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.include_router(api_router)


@app.get("/health", tags=["health"])
async def health(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Liveness + DB connectivity check."""
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
