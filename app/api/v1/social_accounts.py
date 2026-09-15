"""Instagram OAuth: connect a business's Instagram account and start a dashboard session.

Two hops (see the Meta business-login flow):
  GET /instagram/login     -> 302 to Instagram's authorize page (signed `state` carries the source uri)
  GET /instagram/callback  -> Instagram returns here; we exchange the code, persist the
                              account, set an httpOnly session cookie, and 302 to the dashboard.
If the visitor already has a dashboard session (cookie or Bearer), the account is attached
to *their* tenant instead of provisioning a new one ("connect" mode).
Incoming traffic is tracked two ways (kept simple): the source/referrer uri is stored in
social_accounts.metadata, and every attempt is logged to audit_logs (uri, ip, user-agent).
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    ACCESS_TOKEN,
    JWTError,
    create_access_token,
    decode_token,
    create_oauth_state,
    decode_oauth_state,
    encrypt_token,
    set_session_cookie,
)
from app.db.session import get_db
from app.models.role import Role
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.models.user import User
from app.services.audit import record_audit
from app.services.meta import instagram
from app.services.rbac import get_roles_by_names

router = APIRouter(prefix="/social-accounts", tags=["social-accounts"])

PLATFORM = "instagram"


def _client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP, honouring a proxy/tunnel's X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _session_user_id(request: Request) -> Optional[str]:
    """User id from a dashboard session (cookie or Bearer), or None if anonymous/invalid."""
    token = request.cookies.get(settings.session_cookie_name)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:]
    if not token:
        return None
    try:
        payload = decode_token(token)
    except JWTError:
        return None
    return payload.get("sub") if payload.get("type") == ACCESS_TOKEN else None


@router.get("/instagram/login")
async def instagram_login(request: Request) -> RedirectResponse:
    """Redirect the browser to Instagram's consent screen."""
    if not settings.instagram_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Instagram OAuth is not configured (missing INSTAGRAM_APP_ID/SECRET/REDIRECT_URI).",
        )
    # Track where this user came from: explicit ?source= wins, else the Referer header.
    source = request.query_params.get("source") or request.headers.get("referer") or ""
    state = create_oauth_state(source=source[:500], user_id=_session_user_id(request))
    return RedirectResponse(
        instagram.build_authorize_url(state), status_code=status.HTTP_302_FOUND
    )


def _redirect_to_frontend(**query: str) -> RedirectResponse:
    from urllib.parse import urlencode

    url = settings.frontend_url
    if query:
        url = f"{url}?{urlencode(query)}"
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


async def _get_or_create_account(
    db: AsyncSession,
    *,
    ig_user_id: str,
    username: str,
    long_lived_token: str,
    expires_in: int,
    source_uri: str,
    user: Optional[User] = None,
) -> tuple[Tenant, User, SocialAccount]:
    """Find the account by Instagram id, or provision a tenant + owner + social_account.

    With `user` (connect mode) the account is attached to that user's tenant; a new tenant
    is never created and an account already linked to another tenant is refused.
    """
    result = await db.execute(
        select(SocialAccount).where(
            SocialAccount.platform == PLATFORM,
            SocialAccount.external_account_id == ig_user_id,
        )
    )
    account = result.scalar_one_or_none()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    if account is not None and user is not None and account.tenant_id != user.tenant_id:
        raise PermissionError("account_linked_to_other_tenant")

    if account is None and user is not None:
        # Connect mode: attach to the caller's existing tenant.
        tenant = await db.get(Tenant, user.tenant_id)
        account = SocialAccount(
            tenant_id=tenant.id,
            platform=PLATFORM,
            external_account_id=ig_user_id,
            account_name=username,
            access_token_encrypted=encrypt_token(long_lived_token),
            token_expires_at=expires_at,
            meta={"source_uri": source_uri, "username": username},
        )
        db.add(account)
        await db.flush()
        return tenant, user, account

    if account is None:
        # First time this Instagram account connects -> new tenant + owner.
        owner_roles = await get_roles_by_names(db, ["owner"])

        tenant = Tenant(name=username or f"instagram:{ig_user_id}", business_type="instagram")
        db.add(tenant)
        await db.flush()

        owner = User(
            tenant_id=tenant.id,
            name=username or "Instagram Owner",
            email=f"ig_{ig_user_id}@instagram.local",  # IG basic scope gives no email
            username=username,
            verified=True,  # authenticated by Instagram
            roles=owner_roles,
            password_hash=None,  # IG-only login; no local password
        )
        db.add(owner)

        account = SocialAccount(
            tenant_id=tenant.id,
            platform=PLATFORM,
            external_account_id=ig_user_id,
            account_name=username,
            access_token_encrypted=encrypt_token(long_lived_token),
            token_expires_at=expires_at,
            meta={"source_uri": source_uri, "username": username},
        )
        db.add(account)
        await db.flush()
        return tenant, owner, account

    # Returning account -> refresh token + tracking, reuse tenant/owner.
    account.access_token_encrypted = encrypt_token(long_lived_token)
    account.token_expires_at = expires_at
    account.account_name = username or account.account_name
    account.meta = {**(account.meta or {}), "source_uri": source_uri, "username": username}

    tenant = await db.get(Tenant, account.tenant_id)
    owner_res = await db.execute(
        select(User)
        .where(
            User.tenant_id == account.tenant_id,
            User.roles.any(Role.name == "owner"),
        )
        .order_by(User.created_at)
        .limit(1)
    )
    owner = owner_res.scalar_one_or_none()
    if owner is None:  # defensive: tenant with no owner
        owner = User(
            tenant_id=account.tenant_id,
            name=username or "Instagram Owner",
            email=f"ig_{ig_user_id}@instagram.local",
            verified=True,
            roles=await get_roles_by_names(db, ["owner"]),
        )
        db.add(owner)
        await db.flush()
    return tenant, owner, account


@router.get("/instagram/callback")
async def instagram_callback(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> RedirectResponse:
    """Instagram redirects here. Exchange the code, persist, set session cookie, go to dashboard."""
    # User denied consent (or Instagram returned an error).
    if error:
        return _redirect_to_frontend(ig_error=error, ig_error_description=error_description or "")
    if not code or not state:
        return _redirect_to_frontend(ig_error="invalid_request")

    # Validate the signed state (CSRF) and recover the tracked source uri.
    try:
        state_payload = decode_oauth_state(state)
    except JWTError:
        return _redirect_to_frontend(ig_error="invalid_state")
    source_uri = state_payload.get("source", "")
    user = await db.get(User, state_payload["user_id"]) if state_payload.get("user_id") else None
    if user is not None and (user.status != "active" or user.tenant_id is None):
        return _redirect_to_frontend(ig_error="invalid_session")

    # Exchange: code -> short-lived -> long-lived; then read the profile.
    try:
        short = await instagram.exchange_code_for_token(code)
        long_lived = await instagram.exchange_for_long_lived_token(short["access_token"])
        profile = await instagram.fetch_profile(long_lived["access_token"])
    except (httpx.HTTPError, KeyError):
        return _redirect_to_frontend(ig_error="token_exchange_failed")

    ig_user_id = str(profile.get("user_id") or short.get("user_id") or "")
    username = profile.get("username") or ""
    if not ig_user_id:
        return _redirect_to_frontend(ig_error="no_account_id")

    try:
        tenant, owner, account = await _get_or_create_account(
            db,
            ig_user_id=ig_user_id,
            username=username,
            long_lived_token=long_lived["access_token"],
            expires_in=int(long_lived.get("expires_in", 60 * 24 * 3600)),
            source_uri=source_uri,
            user=user,
        )
    except PermissionError as exc:
        return _redirect_to_frontend(ig_error=str(exc))

    # Track this login event (uri + ip + user-agent) in the audit trail.
    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=owner.id,
        action="instagram_account_connected",
        entity_type="social_account",
        entity_id=account.id,
        meta={
            "source_uri": source_uri,
            "ip": _client_ip(request),
            "user_agent": request.headers.get("user-agent"),
            "username": username,
        },
    )
    await db.commit()

    # Mint our own session and hand it to the dashboard via an httpOnly cookie.
    token = create_access_token(
        str(owner.id), tenant_id=tenant.id, roles=[r.name for r in owner.roles]
    )
    response = _redirect_to_frontend(ig_connected="1")
    set_session_cookie(response, token)
    return response
