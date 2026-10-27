"""The business's own online-order system: settings, menu sync, and sending an order from Orders.

Settings and sync are owner/admin; looking at and sending an order is for any member, like the inbox.
Sending stays off (send_enabled) until the business has agreed how test orders are handled: until then
staff see exactly what would be posted and nothing reaches the kitchen.
"""
import uuid
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant, require_role
from app.db.session import get_db
from app.models.order_integration import OrderIntegration
from app.models.sales_lead import SalesLead
from app.schemas.kitchen import Area, CatalogOut, KitchenIn, KitchenOut, KitchenPlan, Line, MenuItem, SendIn
from app.services import kitchen

router = APIRouter(prefix="/kitchen", tags=["kitchen"])
_owner = [Depends(require_role("owner", "admin"))]


def _out(integ: OrderIntegration | None) -> KitchenOut:
    if integ is None:
        return KitchenOut(configured=False)
    return KitchenOut(
        configured=True, base_url=integ.base_url, email=integ.email, send_enabled=integ.send_enabled,
        auto_send=integ.auto_send, synced_at=integ.synced_at, item_count=len(integ.catalog),
        location_count=len(integ.locations),
    )


async def _integration(db: AsyncSession, tenant_id) -> OrderIntegration:
    integ = await db.get(OrderIntegration, tenant_id)
    if integ is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No order system is connected.")
    return integ


async def _sync(db: AsyncSession, integ: OrderIntegration) -> None:
    try:
        await kitchen.sync(db, integ)
    except httpx.HTTPError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not read the menu from the website. Try again.")


@router.get("", response_model=KitchenOut)
async def get_settings(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> KitchenOut:
    return _out(await db.get(OrderIntegration, tenant.id))


@router.put("", response_model=KitchenOut, dependencies=_owner)
async def save_settings(body: KitchenIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> KitchenOut:
    url = body.base_url.strip().rstrip("/")
    if not kitchen.allowed_url(url):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="This order system is not supported yet. Ask Sanchar support to add it.")
    integ = await db.get(OrderIntegration, tenant.id)
    if integ is None:
        integ = OrderIntegration(tenant_id=tenant.id, base_url=url, catalog=[], locations=[])
        db.add(integ)
    moved = integ.base_url != url or not integ.catalog
    integ.base_url, integ.email = url, (body.email or "").strip() or None
    integ.send_enabled, integ.auto_send = body.send_enabled, body.auto_send
    if moved:  # a new connection: read its menu now, so Shop Info and matching have it
        await db.flush()
        await _sync(db, integ)
    await db.commit()
    return _out(integ)


@router.post("/sync", response_model=KitchenOut, dependencies=_owner)
async def sync_now(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> KitchenOut:
    """Re-read the menu and delivery areas now (also rewrites the Shop Info menu topic)."""
    integ = await _integration(db, tenant.id)
    await _sync(db, integ)
    await db.commit()
    return _out(integ)


@router.get("/catalog", response_model=CatalogOut)
async def catalog(tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> CatalogOut:
    """The menu and delivery areas, for the pickers in Orders (the same lists the website shows)."""
    integ = await _integration(db, tenant.id)
    return CatalogOut(
        items=[MenuItem(id=p["_id"], name=p["name"].strip(), price=int(p["price"]), category=str(p.get("category") or ""))
               for p in integ.catalog],
        areas=[Area(**a) for a in integ.locations],
    )


async def _own_lead(db: AsyncSession, tenant_id, lead_id: uuid.UUID) -> SalesLead:
    lead = (
        await db.execute(select(SalesLead).where(SalesLead.id == lead_id, SalesLead.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if lead is None or not (lead.meta or {}).get("order"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No order on this lead.")
    return lead


@router.get("/orders/{lead_id}", response_model=KitchenPlan)
async def order_plan(lead_id: uuid.UUID, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]) -> KitchenPlan:
    """How Sanchar matched this order to the menu, and the exact request it would send."""
    integ = await _integration(db, tenant.id)
    lead = await _own_lead(db, tenant.id, lead_id)
    await kitchen.fresh(db, integ)
    await db.commit()
    order = lead.meta["order"]
    p = kitchen.plan(order, integ)
    body = None
    if p["location"] and all(line["product_id"] for line in p["lines"]):
        lines, loc = kitchen.resolve(integ, [(line["product_id"], line["quantity"]) for line in p["lines"]], p["location"])
        body = kitchen.build_body(order, lines, loc, integ.email)
    return KitchenPlan(
        lines=[Line(**line) for line in p["lines"]], location=p["location"], send_enabled=integ.send_enabled,
        sent=lead.meta["kitchen"] if kitchen.already_sent(lead) else None, error=lead.meta.get("kitchen_error"), body=body,
    )


@router.post("/orders/{lead_id}", response_model=KitchenPlan)
async def send_order(
    lead_id: uuid.UUID, body: SendIn, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> KitchenPlan:
    """Send this order to the business's order system with the items and area staff checked."""
    integ = await _integration(db, tenant.id)
    lead = await _own_lead(db, tenant.id, lead_id)
    if kitchen.already_sent(lead):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This order is already in the kitchen.")
    try:
        lines, loc = kitchen.resolve(integ, [(line.product_id or "", line.quantity) for line in body.lines], body.location)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    payload = kitchen.build_body(lead.meta["order"], lines, loc, integ.email)
    if not integ.send_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Sending to the kitchen is switched off: nothing was sent. An owner can switch it on.")
    try:
        sent = await kitchen.send(db, integ, lead, payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"The order system refused it: {exc.response.text[:300]}")
    except (httpx.HTTPError, ValueError) as exc:  # a timeout may still have placed it: say so
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=(
            f"No clear answer from the order system ({str(exc)[:200] or type(exc).__name__}). "
            "Check its order list before sending again."))
    await db.commit()
    return KitchenPlan(lines=body.lines, location=loc["address"], send_enabled=True, sent=sent, body=payload)
