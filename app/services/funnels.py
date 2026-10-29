"""Conversation funnels: how the bot leads a chat to a result, per kind of business (models/funnel.py).

A funnel is data: its steps go into the prompt, its fields say what the hidden label holds, its triggers say what
runs once the customer confirms. The bot keeps no step counter: each reply, the model reads the chat against the
steps. Code only acts on the labels it writes:
  [summary: ...]  with the summary: replaced by the order total, worked out here from the website menu
  [<label>: ...]  once they confirm: saved as a funnel_submission, then the triggers run
Its checks run before each reply: opening_hours keeps a restaurant from taking orders while it is closed.

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
from app.services import hours, kitchen
from app.services.auto_reply import ORDER_FIELDS, ORDER_STEPS, funnel_block, split_label
from app.services.leads import save_order
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

# save_order: onto the customer's sales lead (the Orders screen) and ping staff. notify: only ping staff.
# website_post: into the business's own order system (kitchen.py), when it has one set to auto-send.
TRIGGERS = ("save_order", "notify", "website_post")
# opening_hours: closed (hours read from Shop Info, services/hours.py) -> no orders; its "message" says when it opens.
CHECKS = ("opening_hours",)
# The same fields confirmed again in the same chat within this window is the bot re-stating the order, not a
# new one. ponytail: time-based; an identical second order inside 3 hours of the first is not sent again.
REPEAT_WINDOW = timedelta(hours=3)

DEFAULT = SimpleNamespace(  # a Funnel's shape, never stored
    key="general", version=0, name="General (orders and bookings)", label="order", steps=ORDER_STEPS,
    fields=list(ORDER_FIELDS), triggers=["save_order", "website_post"], checks=[], active=True,
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


async def closed_message(db: AsyncSession, tenant: Tenant, funnel: Funnel, knowledge: str) -> str | None:
    """Run the funnel's checks: what to tell a customer who wants to order now (the business is closed), or None."""
    for check in funnel.checks:
        if check.get("type") == "opening_hours":
            table = await hours.for_tenant(db, tenant, knowledge)
            message = hours.closed_message(table, check.get("message") or hours.DEFAULT_MESSAGE)
            if message:
                return message
    return None


CONFIRM = "Yo order thik cha? Kei special request bhaye (jastai piro kam, chado chahiyo) pani bhannus."


def order_summary(draft: dict, integ: OrderIntegration | None) -> str:
    """The order summary the customer confirms, always in one layout (the model only writes the label). With the
    business's website menu: each line priced from it, the delivery charge and the total, the way the website will
    charge it; a line or area that doesn't match is shown and asked about, never dropped, and then no total."""
    pickup = bool(re.search(r"\bpick ?up\b", draft["address"], re.IGNORECASE))
    out, problems = ["Tapai ko order:"], []
    if integ is None:
        out += [t.strip() for t in re.split(r"[,;\n]", draft["items"]) if t.strip()]
        out.append("Pick up" if pickup else "Delivery")
    else:
        p, by_id = kitchen.plan(draft, integ), {x["_id"]: x for x in integ.catalog}
        for line in p["lines"]:
            item = by_id.get(line["product_id"])
            out.append(f"{line['quantity']} x {item['name'].strip()} - Rs {item['price']}" if item
                       else f"{line['text']} - menu ma bhetiyena")
            if item is None:
                problems.append(f'"{line["text"]}" menu ma bhetiyena. Kun item bhannu bhayeko ho?')
        if pickup:
            out.append("Pick up (cafe bata)")
        elif p["location"] is not None:
            loc = next(a for a in integ.locations if a["address"] == p["location"])
            out.append(f"Delivery: {loc['address'].title()} (Rs {loc['rate']})")
        else:
            areas = ", ".join(f"{a['address'].title()} (Rs {a['rate']})" for a in integ.locations)
            out.append("Delivery: area thaha bhayena")
            problems.append(f"Delivery kun area ma garne? {areas}")
        if not problems:
            lines, loc = kitchen.resolve(integ, [(x["product_id"], x["quantity"]) for x in p["lines"]], p["location"])
            out.append(f"Total: Rs {kitchen.build_body(draft, lines, loc, None)['totalAmount']}")
    out += ["", *(f"{label}: {draft[k]}" for k, label in (("name", "Naam"), ("phone", "Phone")) if draft.get(k))]
    if not pickup:
        out.append(f"Address: {draft['address']}")
    if draft.get("requests"):
        out.append(f"Special request: {draft['requests']}")
    return "\n".join([*out, "", *(problems or [CONFIRM])])


def plain_summary(funnel: Funnel, draft: dict) -> str:
    """Any other funnel's summary: one "Field: value" line each, then the question."""
    lines = [f"{f['label'].split(',')[0].strip().capitalize()}: {draft[f['key']]}" for f in funnel.fields if f["key"] in draft]
    return "\n".join([*lines, "", "Yo thik cha?"])


async def finish_reply(
    db: AsyncSession, tenant_id, funnel: Funnel, reply: str, closed: str | None = None
) -> tuple[str, dict | None]:
    """The model's reply -> (text for the customer, the fields it collected or None). A complete summary label
    becomes the whole reply, written here (order_summary / plain_summary) so it reads the same every time; an
    incomplete one is just removed. The confirmed label is taken out.
    closed: a check failed (closed_message): a summary or an order the model wrote anyway becomes that message,
    and nothing is collected."""
    m = _SUMMARY.search(reply)
    if closed and (m or split_label(reply, funnel.label, funnel.fields)[1]):
        return closed, None
    if m:
        _, draft = split_label(m.group(0).strip(), "summary", funnel.fields)
        if draft is None:
            reply = re.sub(r"\n{3,}", "\n\n", _SUMMARY.sub("", reply)).strip()
        elif {"items", "address"} <= draft.keys():
            integ = await db.get(OrderIntegration, tenant_id) if "website_post" in funnel.triggers else None
            reply = order_summary(draft, integ if integ is not None and integ.catalog else None)
        else:
            reply = plain_summary(funnel, draft)
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
