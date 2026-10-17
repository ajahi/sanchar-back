"""Instagram OAuth: connect a business's Instagram account and start a dashboard session.

Three hops (see the Meta business-login flow):
  GET /instagram/login     -> 302 to Instagram's authorize page (signed `state` carries the source uri)
  GET /instagram/callback  -> Instagram returns here; we answer at once with a preloader page
                              that forwards to /finish (so the user isn't staring at Instagram).
  GET /instagram/finish    -> exchange the code, persist the account, set an httpOnly
                              session cookie, and 302 to the dashboard.
If the visitor already has a dashboard session (cookie or Bearer), the account is attached
to *their* tenant instead of provisioning a new one ("connect" mode).
Incoming traffic is tracked two ways (kept simple): the source/referrer uri is stored in
social_accounts.metadata, and every attempt is logged to audit_logs (uri, ip, user-agent).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
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
    decrypt_token,
    encrypt_token,
    set_session_cookie,
)
from app.core.tenant import CurrentTenant, CurrentUser
from app.db.session import async_session_factory, get_db
from app.models.role import Role
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.social_account import (
    FacebookLinkIn,
    FacebookPageOut,
    InstagramProfileOut,
    WhatsAppAccountOut,
    WhatsAppLinkIn,
)
from app.services.audit import record_audit
from app.api.v1.webhooks import backfill_recent_conversations
from app.services.meta import facebook, instagram, whatsapp
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


@router.get("/instagram/profile", response_model=list[InstagramProfileOut])
async def instagram_profiles(
    tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[InstagramProfileOut]:
    """Live instagram_business_basic profile of each Instagram account connected to this tenant.
    If Instagram doesn't answer for one (expired token…), that entry falls back to the stored
    id/username with live=False instead of failing the whole page."""
    accounts = (
        await db.execute(
            select(SocialAccount)
            .where(SocialAccount.tenant_id == tenant.id, SocialAccount.platform == PLATFORM)
            .order_by(SocialAccount.created_at)
        )
    ).scalars()
    out = []
    for acc in accounts:
        live: dict = {}
        if acc.access_token_encrypted:
            try:
                live = await instagram.fetch_profile(
                    decrypt_token(acc.access_token_encrypted), instagram.PROFILE_FIELDS
                )
            except httpx.HTTPError as exc:
                logging.getLogger(__name__).warning("instagram profile fetch failed: %r", exc)
        out.append(
            InstagramProfileOut(
                id=acc.external_account_id,
                username=live.get("username") or acc.account_name,
                connected_at=acc.created_at,
                live=bool(live),
                **{
                    k: live.get(k)
                    for k in (
                        "name",
                        "account_type",
                        "profile_picture_url",
                        "followers_count",
                        "follows_count",
                        "media_count",
                    )
                },
            )
        )
    return out


@router.get("/whatsapp", response_model=list[WhatsAppAccountOut])
async def whatsapp_accounts(
    tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[WhatsAppAccountOut]:
    """This tenant's WhatsApp numbers with live status from Meta. Also the Channels page's PING:
    status CONNECTED = token + number work, subscribed_apps = Meta will deliver webhooks,
    last_webhook_at = Meta actually did. If Meta doesn't answer, stored values with live=False."""
    accounts = (
        await db.execute(
            select(SocialAccount)
            .where(SocialAccount.tenant_id == tenant.id, SocialAccount.platform == "whatsapp")
            .order_by(SocialAccount.created_at)
        )
    ).scalars()
    out = []
    for acc in accounts:
        meta = acc.meta or {}
        live: dict = {}
        apps: Optional[list[str]] = None
        if acc.access_token_encrypted:
            token = decrypt_token(acc.access_token_encrypted)
            try:
                live = await whatsapp.fetch_phone_number(
                    token, acc.external_account_id, whatsapp.PHONE_FIELDS
                )
                if meta.get("waba_id"):
                    apps = await whatsapp.fetch_subscribed_apps(token, meta["waba_id"])
            except httpx.HTTPError as exc:
                logging.getLogger(__name__).warning("whatsapp status fetch failed: %r", exc)
        out.append(
            WhatsAppAccountOut(
                phone_number_id=acc.external_account_id,
                display_phone_number=live.get("display_phone_number") or meta.get("display_phone_number"),
                verified_name=live.get("verified_name") or acc.account_name,
                status=live.get("status"),
                quality_rating=live.get("quality_rating"),
                waba_id=meta.get("waba_id"),
                subscribed_apps=apps,
                last_webhook_at=meta.get("last_webhook_at"),
                connected_at=acc.created_at,
                live=bool(live),
            )
        )
    return out


@router.post("/whatsapp", response_model=WhatsAppAccountOut, status_code=status.HTTP_201_CREATED)
async def link_whatsapp(
    body: WhatsAppLinkIn,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WhatsAppAccountOut:
    """Link a WhatsApp number to the logged-in tenant (Channels page → Connect) — the
    scripts/connect_whatsapp.py steps, for whoever is logged in.

    The business first shares its WhatsApp account with ours as a partner, so our platform token
    (WABA_TOKEN) can reach it. Then: the number must be on that account, the account gets
    subscribed to our app's webhooks, and the number is saved for this tenant.
    ponytail: anyone who knows a shared account's two ids can claim a number nobody linked yet;
    a linked number is never moved. Embedded Signup (Meta's own login) replaces this form.
    """
    log = logging.getLogger(__name__)
    token = settings.waba_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="WhatsApp is not configured (WABA_TOKEN)."
        )
    try:
        numbers = await whatsapp.fetch_waba_phone_numbers(token, body.waba_id)
    except httpx.HTTPStatusError as exc:
        log.warning("whatsapp link: no access to %s: %s", body.waba_id, exc.response.text[:300])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sanchar can't access WhatsApp account {body.waba_id}. "
            "Share it with Sanchar as a partner first, then try again.",
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Meta unreachable")
    number = next((n for n in numbers if n.get("id") == body.phone_number_id), None)
    if number is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Phone number ID {body.phone_number_id} is not on WhatsApp account {body.waba_id}.",
        )

    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.platform == "whatsapp",
                SocialAccount.external_account_id == body.phone_number_id,
            )
        )
    ).scalar_one_or_none()
    if account is not None and account.tenant_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This WhatsApp number is already linked to another workspace.",
        )

    try:
        await whatsapp.subscribe_app(token, body.waba_id)
    except httpx.HTTPError as exc:
        body_text = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else ""
        log.error("whatsapp webhook subscription failed: %r %s", exc, body_text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't subscribe the account to Sanchar's webhooks; try again.",
        )

    if account is None:
        account = SocialAccount(
            tenant_id=tenant.id, platform="whatsapp", external_account_id=body.phone_number_id
        )
        db.add(account)
    account.access_token_encrypted = encrypt_token(token)
    account.account_name = number.get("verified_name") or number.get("display_phone_number")
    account.meta = {
        **(account.meta or {}),
        "waba_id": body.waba_id,
        "display_phone_number": number.get("display_phone_number"),
    }
    await db.flush()
    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=user.id,
        action="whatsapp_account_connected",
        entity_type="social_account",
        entity_id=account.id,
        meta={"phone_number_id": body.phone_number_id, "waba_id": body.waba_id},
    )
    await db.commit()
    await db.refresh(account)
    return WhatsAppAccountOut(
        phone_number_id=account.external_account_id,
        display_phone_number=number.get("display_phone_number"),
        verified_name=number.get("verified_name"),
        waba_id=body.waba_id,
        connected_at=account.created_at,
        live=True,
    )


@router.get("/facebook", response_model=list[FacebookPageOut])
async def facebook_pages(
    tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> list[FacebookPageOut]:
    """This tenant's Facebook Pages with live status from Meta. Also the Channels page's PING:
    live = token works, subscribed_fields has "messages" = Meta will deliver webhooks,
    last_webhook_at = Meta actually did. If Meta doesn't answer, stored values with live=False."""
    accounts = (
        await db.execute(
            select(SocialAccount)
            .where(SocialAccount.tenant_id == tenant.id, SocialAccount.platform == "facebook")
            .order_by(SocialAccount.created_at)
        )
    ).scalars()
    own_ig = await _tenant_instagram_ids(db, tenant.id)
    out = []
    for acc in accounts:
        live: dict = {}
        fields: Optional[list[str]] = None
        if acc.access_token_encrypted:
            token = decrypt_token(acc.access_token_encrypted)
            try:
                live = await facebook.fetch_page(token)
                fields = await facebook.fetch_subscribed_fields(token, acc.external_account_id)
            except httpx.HTTPError as exc:
                logging.getLogger(__name__).warning("facebook status fetch failed: %r", exc)
        out.append(
            FacebookPageOut(
                page_id=acc.external_account_id,
                name=live.get("name") or acc.account_name,
                subscribed_fields=fields,
                last_webhook_at=(acc.meta or {}).get("last_webhook_at"),
                linked_instagram_id=(acc.meta or {}).get("linked_instagram_id"),
                instagram_verified=(acc.meta or {}).get("linked_instagram_id") in own_ig,
                connected_at=acc.created_at,
                live=bool(live),
            )
        )
    return out


async def _tenant_instagram_ids(db: AsyncSession, tenant_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(SocialAccount.external_account_id).where(
            SocialAccount.tenant_id == tenant_id, SocialAccount.platform == "instagram"
        )
    )
    return set(rows.scalars())


async def _linked_instagram(db: AsyncSession, tenant_id: uuid.UUID, token: str, page_id: str) -> Optional[str]:
    """Instagram id Meta links to this Page. Raises PermissionError if that Instagram account is
    already connected to ANOTHER workspace: the Page belongs with it, not here."""
    ig_id = await facebook.fetch_linked_instagram(token, page_id)
    if ig_id is None:
        return None
    owner = (
        await db.execute(
            select(SocialAccount.tenant_id).where(
                SocialAccount.platform == "instagram", SocialAccount.external_account_id == ig_id
            )
        )
    ).scalar_one_or_none()
    if owner is not None and owner != tenant_id:
        raise PermissionError("instagram_linked_to_other_workspace")
    return ig_id


async def _audit_facebook(db: AsyncSession, tenant_id, actor_id, account_id, page_id: str) -> None:
    await record_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="facebook_account_connected",
        entity_type="social_account",
        entity_id=account_id,
        meta={"page_id": page_id},
    )


@router.get("/facebook/login")
async def facebook_login(request: Request) -> RedirectResponse:
    """Connect Facebook button: send the logged-in admin to Facebook's consent screen, where they
    pick the Pages to grant. Needs a dashboard session (the Pages attach to that tenant)."""
    if not settings.facebook_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Facebook Login is not configured (missing FACEBOOK_APP_ID/FACEBOOK_REDIRECT_URI/META_APP_SECRET).",
        )
    user_id = _session_user_id(request)
    if user_id is None:
        return _redirect_to_frontend(fb_error="login_required")
    return RedirectResponse(
        facebook.build_authorize_url(create_oauth_state(user_id=user_id)), status_code=status.HTTP_302_FOUND
    )


@router.get("/facebook/callback")
async def facebook_callback(
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    """Facebook returns here: swap the code for Page tokens and link every granted Page to the
    tenant of the admin who clicked Connect (same steps as link_facebook, once per Page)."""
    log = logging.getLogger(__name__)
    if error:
        return _redirect_to_frontend(fb_error=error)
    if not code or not state:
        return _redirect_to_frontend(fb_error="invalid_request")
    try:
        user_id = decode_oauth_state(state).get("user_id")
    except JWTError:
        return _redirect_to_frontend(fb_error="invalid_state")
    user = await db.get(User, user_id) if user_id else None
    if user is None or user.status != "active" or user.tenant_id is None:
        return _redirect_to_frontend(fb_error="invalid_session")

    try:
        pages = await facebook.fetch_user_pages(await facebook.exchange_code_for_long_lived_token(code))
    except (httpx.HTTPError, KeyError) as exc:
        body = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else ""
        log.error("facebook token exchange failed: %r %s", exc, body)
        return _redirect_to_frontend(fb_error="token_exchange_failed")

    linked = 0
    for page in pages:
        page_id, token = page.get("id"), page.get("access_token")
        if not page_id or not token:
            continue
        account = (
            await db.execute(
                select(SocialAccount).where(
                    SocialAccount.platform == "facebook", SocialAccount.external_account_id == page_id
                )
            )
        ).scalar_one_or_none()
        if account is not None and account.tenant_id != user.tenant_id:
            log.warning("facebook page %s already linked to another tenant; skipped", page_id)
            continue
        try:
            ig_id = await _linked_instagram(db, user.tenant_id, token, page_id)
        except PermissionError:
            log.warning("facebook page %s skipped: its Instagram account belongs to another workspace", page_id)
            continue
        try:
            await facebook.subscribe_page(token, page_id)
        except httpx.HTTPError as exc:
            body = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else ""
            log.error("facebook webhook subscription failed for %s: %r %s", page_id, exc, body)
            continue
        if account is None:
            account = SocialAccount(tenant_id=user.tenant_id, platform="facebook", external_account_id=page_id)
            db.add(account)
        account.access_token_encrypted = encrypt_token(token)
        account.account_name = page.get("name")
        account.meta = {**(account.meta or {}), "linked_instagram_id": ig_id}
        await db.flush()
        await _audit_facebook(db, user.tenant_id, user.id, account.id, page_id)
        background_tasks.add_task(_backfill, account.id, token)
        linked += 1
    await db.commit()
    return _redirect_to_frontend(**({"fb_connected": str(linked)} if linked else {"fb_error": "no_pages_linked"}))


@router.post("/facebook", response_model=FacebookPageOut, status_code=status.HTTP_201_CREATED)
async def link_facebook(
    body: FacebookLinkIn,
    background_tasks: BackgroundTasks,
    tenant: CurrentTenant,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FacebookPageOut:
    """Link a Facebook Page's Messenger inbox to the logged-in tenant (Channels page → Connect).

    The token must be a Page access token for `page_id` (Meta answers /me with the Page itself).
    Then: the Page is subscribed to our app's webhooks, saved for this tenant, and its recent
    threads are pulled into the inbox.
    ponytail: the admin pastes a long-lived Page token; Facebook Login (page picker) replaces this
    form when you want one-click connect. A linked Page is never moved to another workspace.
    """
    log = logging.getLogger(__name__)
    token = body.page_access_token
    try:
        page = await facebook.fetch_page(token)
    except httpx.HTTPStatusError as exc:
        log.warning("facebook link: token rejected: %s", exc.response.text[:300])
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Facebook rejected that access token.")
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Meta unreachable")
    if page.get("id") != body.page_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"That token is not a Page access token for Page {body.page_id}.",
        )

    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.platform == "facebook",
                SocialAccount.external_account_id == body.page_id,
            )
        )
    ).scalar_one_or_none()
    if account is not None and account.tenant_id != tenant.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Facebook Page is already linked to another workspace.",
        )

    try:
        ig_id = await _linked_instagram(db, tenant.id, token, body.page_id)
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Page's Instagram account is connected to a different workspace. "
            "Connect the Page from that workspace instead.",
        )
    try:
        await facebook.subscribe_page(token, body.page_id)
    except httpx.HTTPError as exc:
        body_text = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else ""
        log.error("facebook webhook subscription failed: %r %s", exc, body_text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Couldn't subscribe the Page to Sanchar's webhooks; check the token has pages_messaging and pages_manage_metadata.",
        )

    if account is None:
        account = SocialAccount(tenant_id=tenant.id, platform="facebook", external_account_id=body.page_id)
        db.add(account)
    account.access_token_encrypted = encrypt_token(token)
    account.account_name = page.get("name")
    account.meta = {**(account.meta or {}), "linked_instagram_id": ig_id}
    await db.flush()
    await _audit_facebook(db, tenant.id, user.id, account.id, body.page_id)
    await db.commit()
    await db.refresh(account)
    background_tasks.add_task(_backfill, account.id, token)
    return FacebookPageOut(
        page_id=account.external_account_id,
        name=account.account_name,
        subscribed_fields=facebook.SUBSCRIBED_FIELDS.split(","),
        last_webhook_at=(account.meta or {}).get("last_webhook_at"),
        linked_instagram_id=ig_id,
        instagram_verified=ig_id in await _tenant_instagram_ids(db, tenant.id),
        connected_at=account.created_at,
        live=True,
    )


async def _backfill(account_id: uuid.UUID, token: str) -> None:
    """Seed the inbox from Graph. Best-effort; own DB session since the request's is closed."""
    try:
        async with async_session_factory() as db:
            account = await db.get(SocialAccount, account_id)
            if account is not None:
                await backfill_recent_conversations(db, account, token)
                await db.commit()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("conversation backfill failed for account %s", account_id)


async def _after_login(account_id: uuid.UUID, token: str) -> None:
    """Runs after the redirect is sent: webhook subscription, then inbox seed. Each step is
    best-effort and independent."""
    try:
        await instagram.subscribe_to_messages(token)
    except httpx.HTTPError as exc:
        body = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else ""
        logging.getLogger(__name__).error("instagram webhook subscription failed: %r %s", exc, body)
    await _backfill(account_id, token)


# Same look as the frontend's Loader. The browser keeps this page painted while /finish works.
_FINISH_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Sanchar</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:#FAF3E0;color:#1A1A1A}
.ring{width:48px;height:48px;box-sizing:border-box;border:4px solid #000;border-top-color:#B8251B;border-radius:50%;animation:spin 1s linear infinite}
p{font:bold 12px monospace;text-transform:uppercase;letter-spacing:.1em}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body><div class="ring" role="status"></div><p>Igniting Workstation...</p>
<script>setTimeout(function(){location.replace("finish?__QS__")},50)</script></body></html>"""


@router.get("/instagram/callback", response_model=None)
async def instagram_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> RedirectResponse | HTMLResponse:
    """Instagram redirects here. Paint the preloader at once, then hand off to /finish (slow part)."""
    # User denied consent (or Instagram returned an error).
    if error:
        return _redirect_to_frontend(ig_error=error, ig_error_description=error_description or "")
    if not code or not state:
        return _redirect_to_frontend(ig_error="invalid_request")
    # urlencode leaves only [A-Za-z0-9_.~%+-], so nothing here can break out of the JS string.
    # replace() (in the page) keeps this one-time code out of history, so Back can't replay it.
    return HTMLResponse(_FINISH_PAGE.replace("__QS__", urlencode({"code": code, "state": state})))


@router.get("/instagram/finish")
async def instagram_finish(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Optional[str] = None,
    state: Optional[str] = None,
) -> RedirectResponse:
    """Exchange the code, persist, set session cookie, go to dashboard."""
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
    except (httpx.HTTPError, KeyError) as exc:
        body = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else ""
        logging.getLogger(__name__).error("instagram token exchange failed: %r %s", exc, body)
        return _redirect_to_frontend(ig_error="token_exchange_failed", ig_error_description=body[:300])

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

    # After the redirect is sent: subscribe the account to DM webhooks (without it, new DMs never
    # arrive), then seed the inbox (Graph's conversations call takes ~5s; the 5s poll picks it up).
    background_tasks.add_task(_after_login, account.id, long_lived["access_token"])

    # Mint our own session and hand it to the dashboard via an httpOnly cookie.
    token = create_access_token(
        str(owner.id), tenant_id=tenant.id, roles=[r.name for r in owner.roles]
    )
    response = _redirect_to_frontend(ig_connected="1")
    set_session_cookie(response, token)
    return response
