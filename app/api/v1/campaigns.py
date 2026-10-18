"""Campaigns — what each ad promotes (price, one-line blurb, photos) and which ads / ref codes map to it.

Photos are attached from the shop-media panel (PATCH /shop-media/{id} with a campaign_id). Ads register
themselves when a chat opens from an unknown ad; staff map them here once.
"""
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_token
from app.core.tenant import CurrentTenant, require_role
from app.db.session import get_db
from app.models.campaign import Campaign, CampaignLink
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.services.meta import instagram
from app.schemas.campaign import CampaignIn, CampaignOut, CampaignPatch, LinkIn, LinkOut, LinkPatch, PublishIn

router = APIRouter(
    prefix="/campaigns", tags=["campaigns"], dependencies=[Depends(require_role("owner", "admin"))]
)


async def own_campaign(db: AsyncSession, tenant_id: uuid.UUID, campaign_id: uuid.UUID) -> Campaign:
    row = (
        await db.execute(select(Campaign).where(Campaign.id == campaign_id, Campaign.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return row


async def _out(db: AsyncSession, tenant_id: uuid.UUID, rows: list[Campaign]) -> list[CampaignOut]:
    counts = dict(
        (
            await db.execute(
                select(ShopMedia.campaign_id, func.count())
                .where(ShopMedia.tenant_id == tenant_id, ShopMedia.campaign_id.isnot(None))
                .group_by(ShopMedia.campaign_id)
            )
        ).all()
    )
    return [CampaignOut.model_validate(c).model_copy(update={"photo_count": counts.get(c.id, 0)}) for c in rows]


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> list[CampaignOut]:
    rows = (
        await db.execute(select(Campaign).where(Campaign.tenant_id == tenant.id).order_by(Campaign.created_at.desc()))
    ).scalars().all()
    return await _out(db, tenant.id, list(rows))


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> CampaignOut:
    row = Campaign(tenant_id=tenant.id, **body.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return (await _out(db, tenant.id, [row]))[0]


# /links is declared before /{campaign_id} so "links" is never read as an id.
@router.get("/links", response_model=list[LinkOut])
async def list_links(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> list[CampaignLink]:
    """Unmapped ads first (they need staff), then the mapped ones, newest first."""
    rows = await db.execute(
        select(CampaignLink)
        .where(CampaignLink.tenant_id == tenant.id)
        .order_by(CampaignLink.campaign_id.isnot(None), CampaignLink.created_at.desc())
    )
    return list(rows.scalars())


@router.post("/links", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
async def add_link(body: LinkIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> CampaignLink:
    """Register an ad id or ref code by hand (e.g. the `?ref=` code in a reel's ig.me link) and map it."""
    if body.campaign_id:
        await own_campaign(db, tenant.id, body.campaign_id)
    stmt = pg_insert(CampaignLink).values(
        tenant_id=tenant.id, kind=body.kind, external_id=body.external_id.strip(), campaign_id=body.campaign_id
    )
    link_id = await db.scalar(
        stmt.on_conflict_do_update(constraint="uq_campaign_link", set_={"campaign_id": stmt.excluded.campaign_id})
        .returning(CampaignLink.id)
    )
    await db.commit()
    return await db.get(CampaignLink, link_id)


@router.patch("/links/{link_id}", response_model=LinkOut)
async def map_link(
    link_id: uuid.UUID, body: LinkPatch, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> CampaignLink:
    link = (
        await db.execute(select(CampaignLink).where(CampaignLink.id == link_id, CampaignLink.tenant_id == tenant.id))
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
    if body.campaign_id:
        await own_campaign(db, tenant.id, body.campaign_id)
    link.campaign_id = body.campaign_id
    await db.commit()
    await db.refresh(link)
    return link


@router.patch("/{campaign_id}", response_model=CampaignOut)
async def update_campaign(
    campaign_id: uuid.UUID, body: CampaignPatch, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> CampaignOut:
    row = await own_campaign(db, tenant.id, campaign_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None or field in ("price", "blurb", "details"):  # these may be cleared with null
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return (await _out(db, tenant.id, [row]))[0]


@router.post("/{campaign_id}/publish", response_model=CampaignOut)
async def publish_post(
    campaign_id: uuid.UUID, body: PublishIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> CampaignOut:
    """Post one of the campaign's photos to the shop's Instagram feed and keep the post id, so the ad the
    owner makes by boosting that post can be traced back to this campaign."""
    row = await own_campaign(db, tenant.id, campaign_id)
    media = (
        await db.execute(select(ShopMedia).where(ShopMedia.id == body.media_id, ShopMedia.tenant_id == tenant.id))
    ).scalar_one_or_none()
    if media is None or media.source != "upload":  # Instagram fetches the photo, so it must be on our public storage
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Pick a photo uploaded in Sanchar.")
    account = (
        await db.execute(
            select(SocialAccount).where(
                SocialAccount.tenant_id == tenant.id, SocialAccount.platform == "instagram",
                SocialAccount.access_token_encrypted.isnot(None),
            )
        )
    ).scalars().first()
    if account is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Connect your Instagram account first.")
    try:
        post = await instagram.publish_image(
            decrypt_token(account.access_token_encrypted), account.external_account_id, media.url, body.caption.strip()
        )
    except httpx.HTTPStatusError as exc:  # Meta says why (missing permission, bad image...): pass it on
        why = (exc.response.json().get("error") or {}).get("message") if "json" in exc.response.headers.get("content-type", "") else None
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Instagram refused the post: {why or exc.response.status_code}. If it is about permission, "
            "log in with Instagram again so Sanchar may post for you.",
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Instagram is not responding. Try again.")
    row.ig_post_id, row.ig_permalink = post["id"], post["permalink"]
    media.campaign_id = row.id  # the posted photo belongs to the campaign
    await db.commit()
    await db.refresh(row)
    return (await _out(db, tenant.id, [row]))[0]


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: uuid.UUID, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> None:
    """Its ads become unmapped again and its photos stay in the media panel, unassigned (FK SET NULL)."""
    await db.delete(await own_campaign(db, tenant.id, campaign_id))
    await db.commit()
