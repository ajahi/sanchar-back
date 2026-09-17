"""Connecting Meta accounts (Facebook Login for Business) and importing their history.

Three hops:
  GET /social-accounts/facebook/login     -> 302 to Facebook's consent screen
  GET /social-accounts/facebook/callback  -> exchange the code, store one account per
                                             Page that owns an Instagram account, set an
                                             httpOnly session cookie, 302 to the dashboard
  POST /social-accounts/{id}/sync         -> (re)import that account's history

One consent covers every Page the user manages, so a business with several Pages ends up
connected in a single pass rather than one login per account. If a Page is already linked
to a different tenant it is skipped with a warning instead of failing the whole login.

The legacy `/instagram/login` and `/instagram/callback` paths are kept as aliases so a
deployment mid-migration — and any bookmark or Meta dashboard redirect URI — keeps working.

Incoming traffic is tracked two ways (kept simple): the source/referrer uri is stored in
social_accounts.metadata, and every attempt is logged to audit_logs (uri, ip, user-agent).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    ACCESS_TOKEN,
    JWTError,
    create_access_token,
    create_oauth_state,
    decode_oauth_state,
    decode_token,
    encrypt_token,
    set_session_cookie,
)
from app.core.tenant import CurrentTenant
from app.db.session import get_db
from app.models.role import Role
from app.models.social_account import SocialAccount
from app.models.sync_run import SyncRun
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.social_account import SocialAccountOut, SyncRunOut
from app.services import conversation_sync
from app.services.audit import record_audit
from app.services.meta import facebook_login, graph
from app.services.rbac import get_roles_by_names

router = APIRouter(prefix="/social-accounts", tags=["social-accounts"])

PLATFORM = "instagram"
log = logging.getLogger(__name__)

# Without these the account can connect but never read or answer a DM, so a missing scope
# is worth a loud, specific log line rather than a mysterious silent failure later.
# Meta's own Instagram-messaging setup list includes business_management and
# pages_read_engagement; omitting either yields a login that looks fine but reads nothing.
REQUIRED_SCOPES = (
    "instagram_basic",
    "instagram_manage_messages",
    "pages_manage_metadata",
    "pages_show_list",
    "pages_read_engagement",
    "business_management",
)


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


def _redirect_to_frontend(**query: str) -> RedirectResponse:
    from urllib.parse import urlencode

    url = settings.frontend_url
    if query:
        url = f"{url}?{urlencode(query)}"
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


# ------------------------------------------------------------------------------- login


def _begin_login(request: Request) -> RedirectResponse:
    if not facebook_login.configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Facebook Login is not configured "
                "(missing META_APP_ID/META_APP_SECRET/META_REDIRECT_URI)."
            ),
        )
    # Track where this user came from: explicit ?source= wins, else the Referer header.
    source = request.query_params.get("source") or request.headers.get("referer") or ""
    state = create_oauth_state(source=source[:500], user_id=_session_user_id(request))
    return RedirectResponse(
        facebook_login.build_authorize_url(state), status_code=status.HTTP_302_FOUND
    )


@router.get("/facebook/login")
async def facebook_login_start(request: Request) -> RedirectResponse:
    """Redirect the browser to Facebook's consent screen."""
    return _begin_login(request)


@router.get("/instagram/login", deprecated=True)
async def instagram_login(request: Request) -> RedirectResponse:
    """Deprecated alias for /facebook/login, kept so existing links keep working."""
    return _begin_login(request)


# ---------------------------------------------------------------------------- callback


async def _get_or_create_account(
    db: AsyncSession,
    *,
    ig_user_id: str,
    username: str,
    long_lived_token: str,
    expires_in: Optional[int] = None,
    source_uri: str = "",
    user: Optional[User] = None,
    page_id: Optional[str] = None,
    page_name: Optional[str] = None,
) -> tuple[Tenant, User, SocialAccount]:
    """Find the account by Instagram id, or provision a tenant + owner + social_account.

    With `user` (connect mode) the account is attached to that user's tenant; a new tenant
    is never created and an account already linked to another tenant is refused.

    `expires_in=None` stores no expiry: a Page token minted from a long-lived user token
    does not expire, unlike the 60-day Instagram Login token this replaced.
    """
    result = await db.execute(
        select(SocialAccount).where(
            SocialAccount.platform == PLATFORM,
            SocialAccount.external_account_id == ig_user_id,
        )
    )
    account = result.scalar_one_or_none()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in
        else None
    )

    if account is not None and user is not None and account.tenant_id != user.tenant_id:
        raise PermissionError("account_linked_to_other_tenant")

    if account is None and user is not None:
        # Connect mode: attach to the caller's existing tenant.
        tenant = await db.get(Tenant, user.tenant_id)
        account = SocialAccount(
            tenant_id=tenant.id,
            platform=PLATFORM,
            external_account_id=ig_user_id,
            external_page_id=page_id,
            account_name=username,
            access_token_encrypted=encrypt_token(long_lived_token),
            token_expires_at=expires_at,
            meta=_account_meta(source_uri, username, page_id, page_name),
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
            verified=True,  # authenticated by Facebook
            roles=owner_roles,
            password_hash=None,  # social-only login; no local password
        )
        db.add(owner)

        account = SocialAccount(
            tenant_id=tenant.id,
            platform=PLATFORM,
            external_account_id=ig_user_id,
            external_page_id=page_id,
            account_name=username,
            access_token_encrypted=encrypt_token(long_lived_token),
            token_expires_at=expires_at,
            meta=_account_meta(source_uri, username, page_id, page_name),
        )
        db.add(account)
        await db.flush()
        return tenant, owner, account

    # Returning account -> refresh token + tracking, reuse tenant/owner.
    account.access_token_encrypted = encrypt_token(long_lived_token)
    account.token_expires_at = expires_at
    account.account_name = username or account.account_name
    account.external_page_id = page_id or account.external_page_id
    account.meta = {
        **(account.meta or {}),
        **_account_meta(source_uri, username, page_id, page_name),
    }

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


def _account_meta(
    source_uri: str, username: str, page_id: Optional[str], page_name: Optional[str]
) -> dict:
    """Non-secret account metadata (the access token is stored encrypted, never here)."""
    meta: dict = {"source_uri": source_uri, "username": username}
    if page_id:
        meta["page_id"] = page_id
    if page_name:
        meta["page_name"] = page_name
    return meta


@router.get("/facebook/callback")
async def facebook_callback(
    request: Request,
    background: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> RedirectResponse:
    """Facebook redirects here. Exchange the code, store the Pages, start a session."""
    # User denied consent (or Facebook returned an error).
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

    # Exchange: code -> short-lived -> long-lived user token.
    try:
        short = await facebook_login.exchange_code_for_user_token(code)
        long_lived = await facebook_login.exchange_for_long_lived_user_token(
            short["access_token"]
        )
        pages = await facebook_login.fetch_pages(long_lived["access_token"])
    except graph.GraphError as exc:
        log.warning("facebook login exchange failed: %s", exc)
        return _redirect_to_frontend(ig_error="token_exchange_failed")
    except KeyError:
        return _redirect_to_frontend(ig_error="token_exchange_failed")

    if not pages:
        return _redirect_to_frontend(ig_error="no_pages")

    # Record which permissions were actually granted; a missing messaging scope explains
    # most "connected but no messages" reports.
    granted = await _granted_scopes(long_lived.get("access_token", ""))

    connected: list[SocialAccount] = []
    tenant: Optional[Tenant] = None
    owner: Optional[User] = None
    skipped = 0

    for page in pages:
        ig = page.get("instagram_business_account") or {}
        ig_user_id = str(ig.get("id") or "")
        page_id = str(page.get("id") or "")
        page_token = page.get("access_token")
        if not ig_user_id or not page_id or not page_token:
            # Messenger-only Page, or a Page token we were not granted.
            skipped += 1
            continue

        username = ig.get("username") or page.get("name") or ""
        try:
            # The first successful page provisions the tenant; the rest attach to it.
            tenant, owner, account = await _get_or_create_account(
                db,
                ig_user_id=ig_user_id,
                username=username,
                long_lived_token=page_token,
                expires_in=None,  # Page tokens do not expire
                source_uri=source_uri,
                user=owner,
                page_id=page_id,
                page_name=page.get("name"),
            )
        except PermissionError:
            skipped += 1
            log.warning(
                "page %s (ig %s) is linked to another tenant; skipping", page_id, ig_user_id
            )
            continue

        account.meta = {
            **(account.meta or {}),
            "granted_scopes": granted,
            "missing_scopes": [s for s in REQUIRED_SCOPES if granted and s not in granted],
        }
        if granted:
            missing = [s for s in REQUIRED_SCOPES if s not in granted]
            if missing:
                log.warning(
                    "account %s is missing required scopes: %s", ig_user_id, ", ".join(missing)
                )

        # Opt this Page into messaging webhooks. Best-effort: a failure must not block login.
        try:
            account.meta = {
                **account.meta,
                "webhooks": await facebook_login.subscribe_page_webhooks(page_id, page_token),
            }
        except graph.GraphError as exc:
            log.warning("subscribed_apps failed for page %s: %s", page_id, exc)
            account.meta = {**account.meta, "webhooks": {"error": str(exc)}}

        connected.append(account)

    if not connected or tenant is None or owner is None:
        return _redirect_to_frontend(ig_error="no_instagram_account")

    # Track this login event (uri + ip + user-agent) in the audit trail.
    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=owner.id,
        action="meta_account_connected",
        entity_type="social_account",
        entity_id=connected[0].id,
        meta={
            "source_uri": source_uri,
            "ip": _client_ip(request),
            "user_agent": request.headers.get("user-agent"),
            "accounts": [a.external_account_id for a in connected],
            "skipped_pages": skipped,
            "granted_scopes": granted,
        },
    )
    await db.commit()

    # Pull existing history in the background so the inbox is not empty on first load.
    if settings.auto_sync_on_connect:
        for account in connected:
            background.add_task(
                conversation_sync.sync_account_by_id,
                account.id,
                trigger_source="connect",
            )

    # Mint our own session and hand it to the dashboard via an httpOnly cookie.
    token = create_access_token(
        str(owner.id), tenant_id=tenant.id, roles=[r.name for r in owner.roles]
    )
    response = _redirect_to_frontend(
        ig_connected="1", accounts=str(len(connected)), skipped=str(skipped)
    )
    set_session_cookie(response, token)
    return response


@router.get("/instagram/callback", deprecated=True)
async def instagram_callback(
    request: Request,
    background: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> RedirectResponse:
    """Deprecated alias for /facebook/callback, kept for registered redirect URIs."""
    return await facebook_callback(
        request=request,
        background=background,
        db=db,
        code=code,
        state=state,
        error=error,
        error_description=error_description,
    )


async def _granted_scopes(user_token: str) -> list[str]:
    """Read the granted permission list from the token itself; [] if unavailable."""
    if not user_token:
        return []
    try:
        info = await facebook_login.debug_token(user_token)
    except graph.GraphError as exc:
        log.warning("debug_token failed: %s", exc)
        return []
    scopes = info.get("scopes")
    return [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else []


# ----------------------------------------------------------------- connected accounts


@router.get("", response_model=list[SocialAccountOut])
async def list_accounts(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SocialAccount]:
    """Every account connected to the caller's tenant."""
    rows = await db.execute(
        select(SocialAccount)
        .where(SocialAccount.tenant_id == tenant.id)
        .order_by(SocialAccount.created_at)
    )
    return list(rows.scalars().all())


async def _own_account(
    db: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> SocialAccount:
    """Tenant-scoped account fetch — no route loads a tenant-owned row by id alone."""
    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.id == account_id,
                SocialAccount.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Social account not found"
        )
    return account


def _parse_account_id(account_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid account id"
        )


@router.post(
    "/{account_id}/sync",
    response_model=SyncRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_sync(
    account_id: str,
    tenant: CurrentTenant,
    background: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    max_conversations: Annotated[Optional[int], Query(ge=1, le=10000)] = None,
) -> SyncRun:
    """(Re)import this account's conversation history in the background.

    Returns immediately with the `SyncRun` row; poll `/{account_id}/sync-runs` for the
    outcome. Safe to call repeatedly — already-imported messages are skipped.
    """
    parsed = _parse_account_id(account_id)
    account = await _own_account(db, tenant.id, parsed)
    if not account.access_token_encrypted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Account has no stored token"
        )

    # Create the run row now so the caller has something to poll, then execute it.
    run = SyncRun(
        tenant_id=account.tenant_id,
        social_account_id=account.id,
        status="pending",
        trigger_source="manual",
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    background.add_task(
        conversation_sync.sync_account_by_id,
        account.id,
        trigger_source="manual",
        run_id=run.id,
        max_conversations=max_conversations,
    )
    return run


@router.get("/{account_id}/sync-runs", response_model=list[SyncRunOut])
async def list_sync_runs(
    account_id: str,
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SyncRun]:
    """Recent import attempts for this account, newest first."""
    parsed = _parse_account_id(account_id)
    await _own_account(db, tenant.id, parsed)
    rows = await db.execute(
        select(SyncRun)
        .where(SyncRun.social_account_id == parsed, SyncRun.tenant_id == tenant.id)
        .order_by(SyncRun.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())
