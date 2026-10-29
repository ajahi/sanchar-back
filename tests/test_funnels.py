"""Conversation funnels (services/funnels.py): labels read by a funnel's own fields, the summary total, one submission
per confirmation, and a restaurant chat end to end (LLM, channel and the restaurant's website faked). DB parts run in
a rolled-back transaction.

    python -m pytest tests/test_funnels.py
"""
import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.funnel import Funnel, FunnelSubmission
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.order_integration import OrderIntegration
from app.models.sales_lead import SalesLead
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import funnels, kitchen, reply_pipeline
from app.services.auto_reply import build_system_prompt, split_label
from app.services.meta import whatsapp

ENQUIRY = [
    {"key": "name", "label": "name", "required": True, "max": 100},
    {"key": "phone", "label": "phone", "required": True, "max": 50},
    {"key": "need", "label": "what they need", "required": True, "max": 300},
    {"key": "budget", "label": "budget, or none", "required": False, "max": 100},
]
RESTAURANT = [
    {"key": "name", "label": "name", "required": True, "max": 100},
    {"key": "phone", "label": "phone", "required": True, "max": 50},
    {"key": "address", "label": "Pick up, or delivery area and address", "required": True, "max": 300},
    {"key": "items", "label": "items with quantity", "required": True, "max": 500},
    {"key": "requests", "label": "special request, or none", "required": False, "max": 300},
]
MENU = [{"_id": "m1", "name": "Buff Jhol Momo", "price": 250}, {"_id": "m8", "name": "Coke 250ml", "price": 100}]
AREAS = [{"address": "alumaila chowk", "rate": 80}]


def test_a_label_is_read_by_the_funnels_own_fields():
    text, got = split_label("Dhanyabad! [enquiry: Ram | 9812345678 | Facebook ads for my cafe | 50k]", "enquiry", ENQUIRY)
    assert text == "Dhanyabad!" and got == {"name": "Ram", "phone": "9812345678", "need": "Facebook ads for my cafe", "budget": "50k"}
    assert split_label("[enquiry: Ram | 9812345678 | SEO | chaina]", "enquiry", ENQUIRY)[1] == {
        "name": "Ram", "phone": "9812345678", "need": "SEO"}  # "none" in an optional field is left out
    assert split_label("[enquiry: Ram | 9812345678 | none]", "enquiry", ENQUIRY)[1] is None  # a required one is missing
    assert split_label("Naam? [enquiry: name | phone | what they need | budget, or none]", "enquiry", ENQUIRY) == ("Naam?", None)
    assert split_label("[enquiry: Ram | 12 | SEO]", "enquiry", ENQUIRY)[1] is None  # not a phone number
    assert split_label("Hi [order: Ram | 9812345678 | x | y]", "enquiry", ENQUIRY) == ("Hi [order: Ram | 9812345678 | x | y]", None)


def test_the_restaurant_prompt_carries_its_steps_and_both_labels():
    block = funnel_block_for(RESTAURANT)
    assert "[order: name | phone | Pick up, or delivery area and address | items with quantity | special request, or none]" in block
    assert "[summary: name | phone | Pick up, or delivery area and address" in block
    assert "{label}" not in block and "{summary_label}" not in block
    p = build_system_prompt("Momo Rs 150", funnel=block)
    assert "RESTAURANT ORDERS" in p and "ORDERS AND BOOKINGS" not in p and p.index("Momo Rs 150") < p.index("RESTAURANT ORDERS")


def funnel_block_for(fields):
    from importlib import import_module
    seed = import_module("app.db.migrations.versions.0012_funnels")
    return funnels.prompt_block(Funnel(key="restaurant", version=1, label="order", steps=seed.RESTAURANT_STEPS, fields=fields))


async def _chat(monkeypatch) -> None:
    sent: list[str] = []
    script: list[str] = []
    posted: list[dict] = []
    seen: dict = {}

    async def fake_send(token, account_id, to, text):
        sent.append(text)
        return {"message_id": f"wamid.f{uuid.uuid4().hex[:8]}"}

    async def fake_generate(system, history, text, hint=""):
        seen["system"], seen["hint"] = system, hint
        return script.pop(0)

    async def fake_post(db, integ, lead, body):  # the restaurant's website, accepting the order
        posted.append(body)
        lead.meta = {**lead.meta, "kitchen": {"order": lead.meta["order"], "total": body["totalAmount"]}}
        return lead.meta["kitchen"]

    monkeypatch.setattr(whatsapp, "send_text", fake_send)
    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)
    monkeypatch.setattr(kitchen, "send", fake_post)
    monkeypatch.setattr(kitchen, "fresh", lambda *a: asyncio.sleep(0))  # no menu re-sync from the real site

    async with async_session_factory() as db:
        db.commit = db.flush  # stay inside the rolled-back transaction

        class SameSession:  # the kitchen send's own session, here the test's
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(reply_pipeline, "async_session_factory", SameSession)
        try:
            run = uuid.uuid4().hex[:8]
            seed = __import__("importlib").import_module("app.db.migrations.versions.0012_funnels")
            funnel = Funnel(key=f"rest_{run}", version=1, name="Restaurant", label="order", steps=seed.RESTAURANT_STEPS,
                            fields=RESTAURANT, triggers=["save_order", "website_post"], active=True)
            tenant = Tenant(name="Cafe", ai_auto_reply=True, funnel_key=funnel.key)
            db.add_all([funnel, tenant])
            await db.flush()
            db.add(KnowledgeDocument(tenant_id=tenant.id, title="Menu", content="Buff Jhol Momo Rs 250. Coke 250ml Rs 100."))
            db.add(OrderIntegration(tenant_id=tenant.id, base_url="https://site.test/api/v1", send_enabled=True,
                                    auto_send=True, catalog=MENU, locations=AREAS))
            acct = SocialAccount(tenant_id=tenant.id, platform="whatsapp", external_account_id=f"pn-{run}",
                                 access_token_encrypted=encrypt_token("tok"))
            db.add(acct)
            await db.flush()
            cust = Customer(tenant_id=tenant.id, external_user_id=f"977-{run}")
            db.add(cust)
            await db.flush()
            convo = Conversation(tenant_id=tenant.id, customer_id=cust.id, social_account_id=acct.id, channel="whatsapp")
            db.add(convo)
            await db.flush()

            async def say(text: str, bot: str) -> None:
                script.append(bot)
                m = Message(conversation_id=convo.id, sender_type="customer", content=text)
                db.add(m)
                await db.flush()
                await reply_pipeline.auto_reply(db, m.id)

            # 1. the summary: written from the label in one layout (whatever else the model wrote), priced like the website
            await say("duita buff jhol momo ra euta coke 250ml, alumaila chowk delivery, Ram 9812345678",
                      "Thik cha! Ram | 9812345678 | delivery\n"
                      "[summary: Ram | 9812345678 | Alumaila Chowk, near the pump | 2 x Buff Jhol Momo, 1 x Coke 250ml | none]")
            assert "RESTAURANT ORDERS" in seen["system"]  # the tenant's funnel reached the model
            assert sent[-1] == (
                "Tapai ko order:\n2 x Buff Jhol Momo - Rs 250\n1 x Coke 250ml - Rs 100\nDelivery: Alumaila Chowk (Rs 80)\n"
                "Total: Rs 680\n\nNaam: Ram\nPhone: 9812345678\nAddress: Alumaila Chowk, near the pump\n\n" + funnels.CONFIRM)

            # 2. "thik xa": the label is saved as a submission and the order goes to the website
            order_label = "[order: Ram | 9812345678 | Alumaila Chowk, near the pump | 2 x Buff Jhol Momo, 1 x Coke 250ml | piro kam]"
            await say("thik xa, piro kam", f"Dhanyabad! Hamro team le chadai call garnecha. {order_label}")
            assert sent[-1] == "Dhanyabad! Hamro team le chadai call garnecha."
            [sub] = (await db.execute(select(FunnelSubmission).where(FunnelSubmission.tenant_id == tenant.id))).scalars()
            assert (sub.funnel_key, sub.status, sub.fields["requests"]) == (funnel.key, "sent", "piro kam")
            assert [(i["name"], i["quantity"]) for i in posted[-1]["item"]] == [("Buff Jhol Momo", 2), ("Coke 250ml", 1)]
            assert posted[-1]["totalAmount"] == 680
            lead = await db.get(SalesLead, sub.lead_id)
            assert lead.meta["order"]["items"] == "2 x Buff Jhol Momo, 1 x Coke 250ml"  # the Orders screen still reads it

            # 3. the bot re-states the same order a moment later: not saved, not posted again
            await say("ok dhanyabad", f"Hajur! {order_label}")
            assert len(posted) == 1 and len((await db.execute(select(FunnelSubmission).where(FunnelSubmission.tenant_id == tenant.id))).all()) == 1

            # 4. the same order on another day is a new order, and is posted again
            sub.created_at = sub.created_at - timedelta(hours=20)
            await db.flush()
            await say("pheri same order garnu paryo, thik xa", f"Dhanyabad! {order_label}")
            assert len(posted) == 2

            # 5. an item the website doesn't sell: kept in the summary and asked about, no total; after "thik xa" staff are asked
            await say("euta sowrma, pick up", "[summary: Ram | 9812345678 | Pick up | 1 x Sowrma | none]")
            assert sent[-1] == ('Tapai ko order:\n1 x Sowrma - menu ma bhetiyena\nPick up (cafe bata)\n\nNaam: Ram\n'
                                'Phone: 9812345678\n\n"1 x Sowrma" menu ma bhetiyena. Kun item bhannu bhayeko ho?')
            await say("thik xa", "Dhanyabad! [order: Ram | 9812345678 | Pick up | 1 x Sowrma | none]")
            sowrma = (await db.execute(select(FunnelSubmission).where(
                FunnelSubmission.tenant_id == tenant.id, FunnelSubmission.fields["items"].astext == "1 x Sowrma"))).scalar_one()
            assert sowrma.status == "needs_staff" and len(posted) == 2

            # 6. closed (here: every day): the model is told, and a summary or order it writes anyway is refused
            tenant.opening_hours = {"mon": None}
            await db.flush()
            subs = len((await db.execute(select(FunnelSubmission).where(FunnelSubmission.tenant_id == tenant.id))).all())
            await say("duita buff jhol momo, pick up, Ram 9812345678", "[summary: Ram | 9812345678 | Pick up | 2 x Buff Jhol Momo | none]")
            assert "CLOSED" in seen["hint"] and sent[-1] == "Hami ahile banda chau, order lina sakdainau."
            await say("thik xa", "Dhanyabad! [order: Ram | 9812345678 | Pick up | 2 x Buff Jhol Momo | none]")
            assert sent[-1] == "Hami ahile banda chau, order lina sakdainau." and len(posted) == 2
            assert len((await db.execute(select(FunnelSubmission).where(FunnelSubmission.tenant_id == tenant.id))).all()) == subs
            await say("momo kati ho?", "Buff Jhol Momo Rs 250.")  # questions are still answered
            assert sent[-1] == "Buff Jhol Momo Rs 250."
        finally:
            await db.rollback()


def test_restaurant_chat_end_to_end(monkeypatch):
    asyncio.run(_chat(monkeypatch))


def test_summary_layout_without_menu_and_with_an_unknown_area():
    draft = {"name": "Sita", "phone": "9801234567", "address": "Lakeside, gate 2", "items": "2 x Momo, 1 x Coke",
             "requests": "piro kam"}
    assert funnels.order_summary(draft, None) == (  # no website menu: lines as the bot wrote them, no prices or total
        "Tapai ko order:\n2 x Momo\n1 x Coke\nDelivery\n\nNaam: Sita\nPhone: 9801234567\nAddress: Lakeside, gate 2\n"
        "Special request: piro kam\n\n" + funnels.CONFIRM)
    menu = OrderIntegration(catalog=MENU, locations=AREAS)
    got = funnels.order_summary({**draft, "items": "2 x Buff Jhol Momo"}, menu)  # Lakeside is not a delivery area
    assert "Delivery: area thaha bhayena" in got and "Total" not in got
    assert got.endswith("Delivery kun area ma garne? Alumaila Chowk (Rs 80)")
