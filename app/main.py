"""FastAPI application entrypoint for the NepSocial backend."""

import logging

from app.utils.global_exception_handler import global_exception_handler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
# httpx logs every request URL at INFO. Two OAuth calls must carry secrets in the query string (Meta's API),
# so keep those lines out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import api_router
from app.db.session import get_db
from app.services import http_client

@asynccontextmanager
async def lifespan(_: FastAPI):
    http_client.start()
    yield
    await http_client.stop()


app = FastAPI(title="NepSocial Backend", version="0.1.0", lifespan=lifespan)

app.add_exception_handler(Exception, global_exception_handler)
app.include_router(api_router)


@app.get("/health", tags=["health"])
async def health(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Liveness + DB connectivity check."""
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
