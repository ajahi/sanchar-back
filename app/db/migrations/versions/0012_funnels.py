"""funnels: a conversation funnel per kind of business, tenants.funnel_key, funnel_submissions; seeds restaurant v1

Revision ID: 0012_funnels
Revises: 0011_order_integrations
Create Date: 2026-10-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_funnels"
down_revision: Union[str, None] = "0011_order_integrations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Probed against the real model before shipping (scratch probes: quantity asked once, delivery area from the
# list, one-message orders, "ho, piro kam garnus"). Edit through super admin: an edit is a new version.
RESTAURANT_STEPS = """RESTAURANT ORDERS (food and drinks)
- When the customer wants to order (e.g. "momo duita garidinus", "order garna milcha?", "malai yo chahiyo", or they ask about delivery for an item), take the order in these steps, using everything already said in this conversation and asking only for what is still missing. Do not start this with customers who are only asking about prices or the menu.
  1. The exact items. If what they named matches several items in SHOP INFORMATION (a general name like "momo", "chowmein" or "bubble tea" when the menu lists several kinds, flavours, sizes or styles of it), list the options with their prices and ask which one; when there are many, name the kinds and styles with the price range instead of every price. Never choose one for them. If only one item matches, that is the item.
  2. The quantity of each item. A number they said anywhere in this conversation ("duita" = 2, "tinwata" = 3, "euta" = 1, "x2") is the quantity: never ask it again. Only for an item they gave no number for, ask how many, in the same reply as your other questions.
  3. Pick up or delivery. For delivery, if SHOP INFORMATION lists delivery areas, ask which of those areas they are in (name the areas with their delivery charge) and their address there, and use the area name exactly as SHOP INFORMATION writes it. Ask their name and phone number in the same reply. You may ask steps 1 to 3 together.
  4. When you have the items, the quantity of each, pick up or the delivery area and address, their name and phone, send a short summary: each item on its own line like "2 x Buff Jhol Momo - Rs 250" (the price of one, as SHOP INFORMATION writes it), then pick up or the delivery area with its delivery charge, name, phone, and their special request if they gave one. Always end the summary by asking both, e.g. "Yo order thik cha? Kei special request bhaye (jastai piro kam, chado chahiyo) pani bhannus." Then put this at the very end of the reply, filled in with their details: {summary_label}. The customer does not see it: the order total is added to the summary for you. If they change something, send the whole summary again, with this label, and ask again.
  5. Only after they confirm the summary ("ho", "thik cha", "thik xa", "yes", or a yes with a request like "ho, piro kam garnus"): thank them and say the restaurant will call to confirm the order, in their language (e.g. "Dhanyabad! Order confirm garna hamro team le chadai call garnecha."), never that it is already confirmed ("confirm bhayo"). Put this at the very end of your reply, filled in with their details: {label}. The customer does not see it. Never write it before they have confirmed the summary.
- A special request the customer gives at any point ("hold the chillies", "piro kam", "chillo kam", "chado garna saknu hunxa?", "no onion") is part of the order: keep it, put it in the summary and the label in their words, and say the restaurant will see it. Never promise it will be done, and never invent one.
- Quote prices exactly as SHOP INFORMATION writes them: the price of one. Never multiply or add prices and never write a total or "total" (not even "duita ko Rs 500"): the total is added to your summary for you. Never add a unit (per piece, per plate) that SHOP INFORMATION does not say.
- Never say an order is confirmed or paid: the restaurant confirms it.
- Example (the items, prices and areas always come from SHOP INFORMATION): "duita momo" -> ask which momo (the kinds and styles with prices), pick up or delivery, and their name and phone. Then "buff jhol, delivery" plus a listed area, a name and a phone -> the item is 2 Buff Jhol Momo ("duita" already gave the quantity, do not ask it again): send the summary with the summary label and "Yo order thik cha?". Then "thik xa" -> thank them and add the order label."""

RESTAURANT_FIELDS = [
    {"key": "name", "label": "name", "required": True, "max": 100},
    {"key": "phone", "label": "phone", "required": True, "max": 50},
    {"key": "address", "label": "Pick up, or delivery area and address", "required": True, "max": 300},
    {"key": "items", "label": "items with quantity", "required": True, "max": 500},
    {"key": "requests", "label": "special request, or none", "required": False, "max": 300},
]


def upgrade() -> None:
    funnels = op.create_table(
        "funnels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("label", sa.String(30), nullable=False),
        sa.Column("steps", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("triggers", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key", "version", name="uq_funnels_key_version"),
    )
    # at most one live version per funnel
    op.create_index("uq_funnels_active_key", "funnels", ["key"], unique=True, postgresql_where=sa.text("active"))
    op.add_column("tenants", sa.Column("funnel_key", sa.String(50)))
    op.create_table(
        "funnel_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), index=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sales_leads.id", ondelete="SET NULL")),
        sa.Column("funnel_key", sa.String(50), nullable=False),
        sa.Column("funnel_version", sa.Integer(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(30), server_default="new", nullable=False),
        sa.Column("result", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.bulk_insert(funnels, [{
        "key": "restaurant", "version": 1, "name": "Restaurant / Food & Beverage", "label": "order",
        "steps": RESTAURANT_STEPS, "fields": RESTAURANT_FIELDS, "triggers": ["save_order", "website_post"], "active": True,
    }])


def downgrade() -> None:
    op.drop_table("funnel_submissions")
    op.drop_column("tenants", "funnel_key")
    op.drop_index("uq_funnels_active_key", table_name="funnels")
    op.drop_table("funnels")
