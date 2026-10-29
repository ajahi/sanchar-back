"""Conversation funnels: how the bot leads a chat to a result, per kind of business (models/funnel.py).

A funnel is data: its steps go into the prompt, its fields say what the hidden label holds, its triggers say what
runs once the customer confirms. The bot keeps no step counter: each reply, the model reads the chat against the
steps. Code only acts on the labels it writes:
  [summary: ...]  with the summary: replaced by the order total, worked out here from the website menu
  [<label>: ...]  once they confirm: saved as a funnel_submission, then the triggers run

A tenant without a funnel (or whose funnel has no active version) gets DEFAULT, the built-in order steps.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.funnel import Funnel, FunnelSubmission
from app.models.order_integration import OrderIntegration
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import kitchen
from app.services.auto_reply import ORDER_FIELDS, ORDER_STEPS, funnel_block, split_label
from app.services.leads import save_order
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

# save_order: onto the customer's sales lead (the Orders screen) and ping staff. notify: only ping staff.
# website_post: into the business's own order system (kitchen.py), when it has one set to auto-send.
TRIGGERS = ("save_order", "notify", "website_post")
# The same fields confirmed again in the same chat within this window is the bot re-stating the order, not a
# new one. ponytail: time-based; an identical second order inside 3 hours of the first is not sent again.
REPEAT_WINDOW = timedelta(hours=3)

DEFAULT = SimpleNamespace(  # a Funnel's shape, never stored
    key="general", version=0, name="General (orders and bookings)", label="order", steps=ORDER_STEPS,
    fields=list(ORDER_FIELDS), triggers=["save_order", "website_post"], active=True,
)

_SUMMARY = re.compile(r"[ \t]*\[\s*summary\s*:[^\]]*\]", re.IGNORECASE)


async def for_tenant(db: AsyncSession, tenant: Tenant) -> Funnel:
    if not tenant.funnel_key:
        return DEFAULT
    funnel = (
        await db.execute(select(Funnel).where(Funnel.key == tenant.funnel_key, Funnel.active.is_(True)))
    ).scalar_one_or_none()
    if funnel is None:
        log.warning("tenant %s uses funnel %r, which has no active version: default steps", tenant.id, tenant.funnel_key)
        return DEFAULT
    return funnel


def prompt_block(funnel: Funnel) -> str:
    return funnel_block(funnel.steps, funnel.label, funnel.fields)


async def total_line(db: AsyncSession, tenant_id, draft: dict) -> str:
    """"Total: Rs 460 (delivery Rs 80 included)" for a summary's items and area, priced from the business's synced
    website menu, the way the website will price it. "" when there is no menu, or a line or the area doesn't match
    (then staff finish the order from Orders, as before): a total is never guessed."""
    integ = await db.get(OrderIntegration, tenant_id)
    if integ is None or not integ.catalog or not draft.get("items") or not draft.get("address"):
        return ""
    p = kitchen.plan(draft, integ)
    if p["location"] is None or any(line["product_id"] is None for line in p["lines"]):
        log.info("summary total left out for tenant %s: %s, area %s", tenant_id,
                 [(line["text"], line["product_id"] is not None) for line in p["lines"]], p["location"])
        return ""
    lines, loc = kitchen.resolve(integ, [(line["product_id"], line["quantity"]) for line in p["lines"]], p["location"])
    body = kitchen.build_body(draft, lines, loc, None)
    charge = body["deliveryCharge"]
    return f"Total: Rs {body['totalAmount']}" + (f" (delivery Rs {charge} included)" if charge else "")


async def finish_reply(db: AsyncSession, tenant_id, funnel: Funnel, reply: str) -> tuple[str, dict | None]:
    """The model's reply -> (text for the customer, the fields it collected or None): the summary label (written at
    the end, where the model writes labels reliably) is taken out and, for website_post funnels, the total goes in
    above the summary's last line (its "Yo order thik cha?"); the confirmed label is taken out."""
    m = _SUMMARY.search(reply)
    if m:
        _, draft = split_label(m.group(0).strip(), "summary", funnel.fields)
        line = await total_line(db, tenant_id, draft) if draft and "website_post" in funnel.triggers else ""
        reply = re.sub(r"\n{3,}", "\n\n", _SUMMARY.sub("", reply)).strip()
        if line:
            lines = reply.split("\n")
            lines.insert(len(lines) - 1, line) if len(lines) > 1 else lines.append(line)
            reply = "\n".join(lines)
    return split_label(reply, funnel.label, funnel.fields)


async def record(
    db: AsyncSession, *, funnel: Funnel, account: SocialAccount, convo: Conversation, customer: Customer, fields: dict
) -> FunnelSubmission | None:
    """Save what the customer confirmed and run the save_order / notify triggers (website_post runs after the
    commit: reply_pipeline). None when it repeats this chat's last submission (the bot re-stating it). Adds, never commits."""
    last = (
        await db.execute(
            select(FunnelSubmission)
            .where(FunnelSubmission.conversation_id == convo.id)
            .order_by(FunnelSubmission.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None and last.fields == fields and datetime.now(timezone.utc) - last.created_at < REPEAT_WINDOW:
        return None
    sub = FunnelSubmission(
        tenant_id=account.tenant_id, conversation_id=convo.id, customer_id=customer.id,
        funnel_key=funnel.key, funnel_version=funnel.version, fields=fields, result={},
    )
    db.add(sub)
    if "save_order" in funnel.triggers and {"name", "phone", "items"} <= fields.keys():
        lead = await save_order(db, account=account, convo=convo, customer=customer, order=fields)
        await db.flush()
        sub.lead_id = lead.id
    if "notify" in funnel.triggers:
        who = fields.get("name") or customer.name or "A customer"
        await create_notification(
            db, event="funnel_submitted", tenant_id=account.tenant_id, source_table="conversations", entity_id=convo.id,
            subject=f"New {funnel.label} from {who}",
            message="; ".join(f"{f['key']}: {fields[f['key']]}" for f in funnel.fields if f["key"] in fields),
            payload={"conversation_id": str(convo.id), "customer_id": str(customer.id)},
        )
    await db.flush()
    return sub
