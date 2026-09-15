"""FastAPI application entrypoint for the NepSocial backend."""
from typing import Annotated

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import api_router
from app.db.session import get_db

app = FastAPI(title="NepSocial Backend", version="0.1.0")

app.include_router(api_router)


@app.get("/health", tags=["health"])
async def health(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Liveness + DB connectivity check."""
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
