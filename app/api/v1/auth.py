"""Authentication endpoints — login and refresh."""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    JWTError,
    clear_session_cookie,
    EMAIL_VERIFY,
    create_access_token,
    create_email_verify_token,
    hash_password,
    set_session_cookie,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.db.session import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    AccessToken,
    EmailRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    VerifyRequest,
)
from app.services.email import send_verification_email
from app.services.rbac import get_roles_by_names

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> TokenPair:
    """Exchange email (as `username`) + password for an access/refresh token pair.

    The access token is also set as the httpOnly session cookie for the dashboard.
    """
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
    if not user.verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please confirm your email first. Check your inbox for the link.",
        )

    access = create_access_token(
        str(user.id), tenant_id=user.tenant_id, roles=[r.name for r in user.roles]
    )
    set_session_cookie(response, access)
    return TokenPair(
        access_token=access,
        refresh_token=create_refresh_token(str(user.id), tenant_id=user.tenant_id),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    """Clear the dashboard session cookie (JWTs themselves are stateless)."""
    clear_session_cookie(response)


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


def _queue_verification(bg: BackgroundTasks, user: User) -> None:
    bg.add_task(
        send_verification_email, user.email, user.name, create_email_verify_token(str(user.id))
    )


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
async def register(
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    bg: BackgroundTasks,
) -> dict:
    """Create tenant + owner (unverified) and email a confirmation link. No session yet."""
    email = str(body.email).lower()
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.verified:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
            )
        _queue_verification(bg, existing)  # never confirmed -> just resend
        return {"detail": "Confirmation email sent"}

    tenant = Tenant(name=body.business_name, owner_name=body.name)
    db.add(tenant)
    await db.flush()
    user = User(
        tenant_id=tenant.id,
        name=body.name,
        email=email,
        password_hash=hash_password(body.password),
        roles=await get_roles_by_names(db, ["owner"]),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:  # raced with another signup for the same email
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    _queue_verification(bg, user)
    return {"detail": "Confirmation email sent"}


@router.post("/resend-verification")
async def resend_verification(
    body: EmailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    bg: BackgroundTasks,
) -> dict:
    """Always 200, so it can't be used to probe which emails are registered."""
    user = (
        await db.execute(select(User).where(User.email == str(body.email).lower()))
    ).scalar_one_or_none()
    if user is not None and not user.verified and user.status == "active":
        _queue_verification(bg, user)
    return {"detail": "If that account needs confirming, an email is on its way"}


@router.post("/verify-email", response_model=TokenPair)
async def verify_email(
    body: VerifyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    response: Response,
) -> TokenPair:
    """Confirm the address from the emailed token and sign the user in."""
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This confirmation link is invalid or has expired",
    )
    try:
        payload = decode_token(body.token)
    except JWTError:
        raise invalid
    if payload.get("type") != EMAIL_VERIFY:
        raise invalid
    user = await db.get(User, payload.get("sub"))
    if user is None or user.status != "active":
        raise invalid

    user.verified = True
    await db.commit()
    access = create_access_token(
        str(user.id), tenant_id=user.tenant_id, roles=[r.name for r in user.roles]
    )
    set_session_cookie(response, access)
    return TokenPair(
        access_token=access,
        refresh_token=create_refresh_token(str(user.id), tenant_id=user.tenant_id),
    )
