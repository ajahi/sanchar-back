"""Orders / sales leads — every customer who gave a phone number, with the order the bot collected (if any).

Staff go through the new ones (typically at the end of the day), call, and mark each confirmed or cancelled.
Any member of the business can, like the inbox. Always scoped to the caller's tenant.
"""
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import CurrentTenant
from app.db.session import get_db
from app.models.customer import Customer
from app.models.sales_lead import SalesLead
from app.schemas.lead import LeadOut, LeadPatch, LeadStatus
from app.services import kitchen

router = APIRouter(prefix="/leads", tags=["leads"])

MAX_LISTED = 200  # ponytail: no paging; newest first, past this staff filter by status


def _out(lead: SalesLead, customer: Customer) -> LeadOut:
    return LeadOut(
        id=lead.id, status=lead.status, phone=lead.phone, channel=lead.channel,
        customer_name=customer.name or customer.external_username, conversation_id=lead.conversation_id,
        order=(lead.meta or {}).get("order"), updated_at=lead.updated_at,
        kitchen=lead.meta["kitchen"] if kitchen.already_sent(lead) else None,
        kitchen_error=(lead.meta or {}).get("kitchen_error"),
    )


@router.get("", response_model=list[LeadOut])
async def list_leads(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
    status_: Annotated[Optional[LeadStatus], Query(alias="status")] = None,
) -> list[LeadOut]:
    """Orders first (a lead with only a phone is less ready), each group newest first."""
    q = (
        select(SalesLead, Customer)
        .join(Customer, Customer.id == SalesLead.customer_id)
        .where(SalesLead.tenant_id == tenant.id)
        .order_by(SalesLead.meta["order"].is_(None), SalesLead.updated_at.desc())
        .limit(MAX_LISTED)
    )
    if status_:
        q = q.where(SalesLead.status == status_)
    return [_out(lead, cu) for lead, cu in (await db.execute(q)).all()]


@router.patch("/{lead_id}", response_model=LeadOut)
async def set_status(
    lead_id: uuid.UUID, body: LeadPatch, tenant: CurrentTenant, db: Annotated[AsyncSession, Depends(get_db)]
) -> LeadOut:
    row = (
        await db.execute(
            select(SalesLead, Customer)
            .join(Customer, Customer.id == SalesLead.customer_id)
            .where(SalesLead.id == lead_id, SalesLead.tenant_id == tenant.id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    lead, customer = row
    lead.status = body.status
    await db.commit()
    await db.refresh(lead)
    return _out(lead, customer)
