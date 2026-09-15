"""Authentication endpoints — login and refresh."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    JWTError,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import AccessToken, RefreshRequest, TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenPair:
    """Exchange email (as `username`) + password for an access/refresh token pair."""
    result = await db.execute(
        select(User).where(User.email == form.username)
    )
    user = result.scalar_one_or_none()

    if (
        user is None
        or not user.password_hash
        or not verify_password(form.password, user.password_hash)
        or user.status != "active"
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenPair(
        access_token=create_access_token(
            str(user.id), tenant_id=user.tenant_id, roles=[r.name for r in user.roles]
        ),
        refresh_token=create_refresh_token(str(user.id), tenant_id=user.tenant_id),
    )


@router.post("/refresh", response_model=AccessToken)
async def refresh(
    body: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccessToken:
    """Issue a fresh access token from a valid refresh token."""
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(body.refresh_token)
    except JWTError:
        raise invalid
    if payload.get("type") != REFRESH_TOKEN:
        raise invalid

    user_id = payload.get("sub")
    if not user_id:
        raise invalid

    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        raise invalid

    return AccessToken(
        access_token=create_access_token(
            str(user.id), tenant_id=user.tenant_id, roles=[r.name for r in user.roles]
        )
    )
