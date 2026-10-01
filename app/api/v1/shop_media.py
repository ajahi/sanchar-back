"""Shop media — the images admins reference and send to customers, and the in-stock flag.

Two sources in one list: the page's newest Instagram posts (only links are kept; Instagram's signed
CDN links expire, so they are re-fetched at most every REFRESH_SECONDS) and files admins upload to
object storage (permanent, public, safe for Meta to fetch when we send them).
"""
import logging
import time
import uuid
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_token
from app.core.tenant import CurrentTenant, require_role
from app.db.session import get_db
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.schemas.shop_media import ShopMediaOut, ShopMediaPatch
from app.services import storage
from app.services.meta import instagram

log = logging.getLogger(__name__)
router = APIRouter(prefix="/shop-media", tags=["shop-media"])

IG_SHOWN = 9  # newest Instagram posts shown in the panel
REFRESH_SECONDS = 900
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_last_sync: dict[uuid.UUID, float] = {}  # ponytail: per-process; with several workers each refreshes on its own


def _sniff(data: bytes) -> tuple[str, str] | None:
    """(extension, content type) from the file's own bytes; the client's content-type is not trusted."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


async def _sync_instagram(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Upsert the page's newest posts (url, caption, permalink); title and in_stock are the admin's and stay."""
    if time.monotonic() - _last_sync.get(tenant_id, float("-inf")) < REFRESH_SECONDS:
        return
    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.tenant_id == tenant_id,
                SocialAccount.platform == "instagram",
                SocialAccount.status == "active",
                SocialAccount.access_token_encrypted.isnot(None),
            )
        )
    ).scalars().first()
    if account is None:
        return
    try:
        items = await instagram.fetch_media(decrypt_token(account.access_token_encrypted), IG_SHOWN)
    except httpx.HTTPError:
        log.warning("instagram media fetch failed for tenant %s", tenant_id, exc_info=True)
        return  # show what is stored; try again on the next load
    _last_sync[tenant_id] = time.monotonic()

    for it in items:
        kind = it.get("media_type", "IMAGE")
        url = it.get("thumbnail_url") if kind == "VIDEO" else it.get("media_url")
        if not url:
            continue
        caption = it.get("caption")
        posted = it.get("timestamp")
        stmt = pg_insert(ShopMedia).values(
            tenant_id=tenant_id,
            source="instagram",
            ig_media_id=it["id"],
            media_type=kind,
            url=url,
            caption=caption,
            permalink=it.get("permalink"),
            posted_at=datetime.strptime(posted, "%Y-%m-%dT%H:%M:%S%z") if posted else None,
            title=(caption or "").strip().split("\n")[0][:80] or None,
        )
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_shop_media_ig",
                set_={"url": stmt.excluded.url, "caption": stmt.excluded.caption,
                      "permalink": stmt.excluded.permalink, "media_type": stmt.excluded.media_type},
            )
        )
    await db.commit()


async def own_media(db: AsyncSession, tenant_id: uuid.UUID, media_id: uuid.UUID) -> ShopMedia:
    row = (
        await db.execute(select(ShopMedia).where(ShopMedia.id == media_id, ShopMedia.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    return row


@router.get("", response_model=list[ShopMediaOut])
async def list_media(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> list[ShopMedia]:
    await _sync_instagram(db, tenant.id)
    # ponytail: older posts stay stored (their sold-out flag still reaches the bot) but only the newest
    # IG_SHOWN are listed; a post deleted on Instagram lingers until newer posts push it out.
    ig = (
        await db.execute(
            select(ShopMedia)
            .where(ShopMedia.tenant_id == tenant.id, ShopMedia.source == "instagram")
            .order_by(ShopMedia.posted_at.desc().nulls_last())
            .limit(IG_SHOWN)
        )
    ).scalars().all()
    uploads = (
        await db.execute(
            select(ShopMedia)
            .where(ShopMedia.tenant_id == tenant.id, ShopMedia.source == "upload")
            .order_by(ShopMedia.created_at.desc())
        )
    ).scalars().all()
    return [*uploads, *ig]


@router.post(
    "",
    response_model=ShopMediaOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def upload_media(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(max_length=255)] = "",
    caption: Annotated[str, Form(max_length=2000)] = "",
) -> ShopMedia:
    if not storage.configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Image storage is not configured")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image is larger than 5 MB")
    kind = _sniff(data)
    if kind is None:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only JPG, PNG or WebP images")
    ext, content_type = kind

    key = f"public/uploads/{tenant.id}/{uuid.uuid4().hex}.{ext}"
    try:
        url = await storage.upload(key, data, content_type)
    except Exception:  # noqa: BLE001 — boto raises many types; the admin only needs to know it failed
        log.exception("image upload to storage failed")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not store the image")

    row = ShopMedia(
        tenant_id=tenant.id, source="upload", file_key=key, url=url,
        title=title.strip() or None, caption=caption.strip() or None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.patch("/{media_id}", response_model=ShopMediaOut)
async def update_media(
    media_id: uuid.UUID,
    body: ShopMediaPatch,
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ShopMedia:
    row = await own_media(db, tenant.id, media_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete(
    "/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def delete_media(
    media_id: uuid.UUID, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    row = await own_media(db, tenant.id, media_id)
    if row.source != "upload":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Instagram posts can't be deleted here")
    key = row.file_key
    await db.delete(row)
    await db.commit()
    if key:
        try:
            await storage.delete(key)
        except Exception:  # noqa: BLE001 — the row is gone; an orphaned file is harmless
            log.warning("could not delete stored image %s", key, exc_info=True)
