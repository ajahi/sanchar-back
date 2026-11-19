"""The auto-reply pipeline against Postgres (always rolled back), with the LLM and the channel faked.

    python -m pytest tests/test_reply_pipeline.py
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import funnels, reply_pipeline
from app.services.meta import whatsapp

NOW = datetime.now(timezone.utc)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


async def _run(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    script: list = []  # what the fake LLM returns next (a string, or an exception to raise)
    seen: dict = {}
    hooks: list = []  # awaited inside the fake LLM call: simulates things that happen while it generates

    async def fake_send(token, account_id, to, text):
        sent.append((to, text))
        return {"message_id": f"wamid.ai{len(sent)}"}

    async def fake_generate(system, history, text, hint=""):
        seen.update(system=system, history=history, text=text)
        if hooks:
            await hooks.pop()()
        nxt = script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(whatsapp, "send_text", fake_send)
    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)

    async with async_session_factory() as db:
        tenant = Tenant(name="Shop", ai_auto_reply=True)
        empty = Tenant(name="No context", ai_auto_reply=True)
        db.add_all([tenant, empty])
        await db.flush()
        db.add(KnowledgeDocument(tenant_id=tenant.id, title="Shop", content="Chura CH-001 Rs 300"))
        acct = SocialAccount(tenant_id=tenant.id, platform="whatsapp", external_account_id="pn-1",
                             access_token_encrypted=encrypt_token("tok"))
        acct2 = SocialAccount(tenant_id=empty.id, platform="whatsapp", external_account_id="pn-2",
                              access_token_encrypted=encrypt_token("tok"))
        db.add_all([acct, acct2])
        await db.flush()

        async def new_chat(t, a, n):
            cust = Customer(tenant_id=t.id, external_user_id=f"977980000000{n}")
            db.add(cust)
            await db.flush()
            convo = Conversation(tenant_id=t.id, customer_id=cust.id, social_account_id=a.id, channel="whatsapp")
            db.add(convo)
            await db.flush()
            return convo

        def add(convo, sender, text, minutes_ago):
            m = Message(conversation_id=convo.id, sender_type=sender, content=text, created_at=ago(minutes_ago))
            db.add(m)
            return m

        # 1. plain reply: sent to the customer, stored under the channel message id as ours, context reached the model
        c1 = await new_chat(tenant, acct, 1)
        m1 = add(c1, "customer", "hello", 30)
        await db.flush()
        script.append("Hi! How can I help?")
        await reply_pipeline.auto_reply(db, m1.id)
        assert sent == [("9779800000001", "Hi! How can I help?")] and "Chura CH-001 Rs 300" in seen["system"]
        ai = (await db.execute(select(Message).where(Message.external_message_id == "wamid.ai1"))).scalar_one()
        assert ai.sender_type == "ai" and ai.ai_generated
        ai.created_at = ago(29)  # the fake timeline is in the past; real replies precede the next message

        # 2. the next message sees the earlier turns, oldest first
        m2 = add(c1, "customer", "price of CH-001?", 28)
        await db.flush()
        script.append("Rs 300.")
        await reply_pipeline.auto_reply(db, m2.id)
        assert len(sent) == 2 and seen["history"] == [("customer", "hello"), ("assistant", "Hi! How can I help?")]

        # 3. a person replied a minute ago -> the bot stays quiet; once they have been idle past the window it resumes
        c3 = await new_chat(tenant, acct, 3)
        human = add(c3, "agent", "Namaste, I will help you.", 1)
        m3 = add(c3, "customer", "delivery?", 0.5)
        await db.flush()
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 2
        human.created_at = ago(reply_pipeline.settings.human_pause_minutes + 1)
        script.append("Rs 100 inside the city.")
        await reply_pipeline.auto_reply(db, m3.id)
        assert len(sent) == 3

        # 4. a person jumps in WHILE the model is generating -> the reply is dropped
        c4 = await new_chat(tenant, acct, 4)
        m4 = add(c4, "customer", "hello?", 5)
        await db.flush()

        async def human_steps_in():
            db.add(Message(conversation_id=c4.id, sender_type="agent", content="I am here", created_at=ago(0)))
            await db.flush()

        hooks.append(human_steps_in)
        script.append("should never be sent")
        await reply_pipeline.auto_reply(db, m4.id)
        assert len(sent) == 3

        # 5. AI switched off -> silent
        c5 = await new_chat(tenant, acct, 5)
        m5 = add(c5, "customer", "hi", 5)
        await db.flush()
        tenant.ai_auto_reply = False
        await reply_pipeline.auto_reply(db, m5.id)
        assert len(sent) == 3
        tenant.ai_auto_reply = True

        # 6. model down (both tries) -> nothing sent, nothing raised
        script.extend([httpx.ConnectError("down"), httpx.ConnectError("down")])
        await reply_pipeline.auto_reply(db, m5.id)
        assert len(sent) == 3

        # 7. a tenant with no shop context is never answered by the bot
        c7 = await new_chat(empty, acct2, 7)
        m7 = add(c7, "customer", "hello", 5)
        await db.flush()
        await reply_pipeline.auto_reply(db, m7.id)
        assert len(sent) == 3
        await db.rollback()


def test_reply_pipeline(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))


def test_ai_sends_photos_it_picks(monkeypatch) -> None:
    """The model sees this shop's in-stock named photos as P-labels; the labels it writes become photos sent after
    its text. Unique ids (no clash with other test data); runs in a rolled-back transaction."""
    import uuid

    from app.models.shop_media import ShopMedia

    run = uuid.uuid4().hex[:8]
    texts: list[str] = []
    images: list[str] = []
    seen: dict = {}
    script: list[str] = []
    fail = {"images": False}

    async def fake_send(token, account_id, to, text):
        texts.append(text)
        return {"message_id": f"wamid.t-{run}-{len(texts)}"}

    async def fake_image(token, account_id, to, url):
        if fail["images"]:
            raise httpx.ConnectError("meta down")
        images.append(url)
        return {"message_id": f"wamid.i-{run}-{len(images)}"}

    async def fake_generate(system, history, text, hint=""):
        seen.update(system=system, history=history)
        return script.pop(0)

    monkeypatch.setattr(whatsapp, "send_text", fake_send)
    monkeypatch.setattr(whatsapp, "send_image", fake_image)
    monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)

    async def go() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction
            shop, other = Tenant(name="Photo shop", ai_auto_reply=True), Tenant(name="Other shop")
            db.add_all([shop, other])
            await db.flush()
            db.add(KnowledgeDocument(tenant_id=shop.id, title="Shop", content="Red Kurtha Rs 1800"))
            acct = SocialAccount(tenant_id=shop.id, platform="whatsapp", external_account_id=f"pn-{run}",
                                 access_token_encrypted=encrypt_token("tok"))
            db.add_all([
                acct,
                ShopMedia(tenant_id=shop.id, source="upload", url="https://s/kurtha.jpg", title="Red Kurtha",
                          tags=["red", "kurtha", "tihar"], created_at=ago(5)),
                ShopMedia(tenant_id=shop.id, source="upload", url="https://s/chura.jpg", title="Two Row Chura Box",
                          tags=["bhai-tika gifts"], in_stock=False, created_at=ago(4)),  # sold out: never listed
                ShopMedia(tenant_id=shop.id, source="upload", url="https://s/blank.jpg", created_at=ago(3)),  # no name/tags
                # an imported Instagram post named only by its caption: not a product until an admin tags it
                ShopMedia(tenant_id=shop.id, source="instagram", ig_media_id=f"ig-{run}", url="https://cdn.ig/x.jpg",
                          title="Blue me away.", created_at=ago(2)),
                ShopMedia(tenant_id=other.id, source="upload", url="https://s/theirs.jpg", title="Their Saree"),
            ])
            cust = Customer(tenant_id=shop.id, external_user_id=f"977-{run}")
            db.add(cust)
            await db.flush()
            convo = Conversation(tenant_id=shop.id, customer_id=cust.id, social_account_id=acct.id, channel="whatsapp")
            db.add(convo)
            await db.flush()

            async def ask(text: str, minutes: float):
                m = Message(conversation_id=convo.id, sender_type="customer", content=text, created_at=ago(minutes))
                db.add(m)
                await db.flush()
                await reply_pipeline.auto_reply(db, m.id)

            # 1. text, then the picked photo; an unknown label sends nothing
            script.append("Cha! Red Kurtha Rs 1800 ho. [photo:P1] [photo:P7]")
            await ask("red kurtha cha?", 3)
            assert "P1 Red Kurtha [red, kurtha, tihar]" in seen["system"]
            # the sold-out box is only named as sold out (build_knowledge), never offered as a photo
            assert "Two Row Chura Box [" not in seen["system"] and "SOLD OUT right now" in seen["system"]
            assert "Their Saree" not in seen["system"] and "Blue me away" not in seen["system"]
            assert "\nP2 " not in seen["system"]
            assert texts == ["Cha! Red Kurtha Rs 1800 ho."] and images == ["https://s/kurtha.jpg"]
            sent_photo = (
                await db.execute(select(Message).where(Message.external_message_id == f"wamid.i-{run}-1"))
            ).scalar_one()
            assert (sent_photo.sender_type, sent_photo.message_type, sent_photo.content) == (
                "ai", "image", "[sent photo: Red Kurtha]")
            for m in (await db.execute(select(Message).where(Message.conversation_id == convo.id, Message.sender_type == "ai"))).scalars():
                m.created_at = ago(2.5)  # the fake timeline is in the past; replies precede the next message

            # 2. the model remembers it showed the photo; a photo-only answer sends no empty text
            script.append("[photo:P1]")
            await ask("feri dekhau na", 2)
            assert ("assistant", "[sent photo: Red Kurtha]") in seen["history"]
            assert len(texts) == 1 and images == ["https://s/kurtha.jpg"] * 2

            # 3. the photo send fails -> the text is still delivered and stored
            fail["images"] = True
            script.append("Yo ho. [photo:P1]")
            await ask("photo?", 1)
            assert texts[-1] == "Yo ho." and len(images) == 2

            # 4. the bot collected an order: the label never reaches the customer, the order lands on the lead.
            # The kitchen send blowing up (here: its table missing after a deploy) costs neither of them.
            from app.models.sales_lead import SalesLead

            class KitchenDown:
                async def __aenter__(self):
                    raise RuntimeError('relation "order_integrations" does not exist')

                async def __aexit__(self, *exc):
                    return False

            async def kitchen_down(*_a, **_k):
                raise RuntimeError('relation "order_integrations" does not exist')

            monkeypatch.setattr(reply_pipeline, "async_session_factory", KitchenDown)
            monkeypatch.setattr(reply_pipeline.kitchen, "auto_send", kitchen_down)

            script.append("Dhanyabad Sita ji! Hami call garera confirm garchau. [order: Sita | 9801234567 | Patan | 1 Red Kurtha]")
            await ask("Sita, 9801234567, Patan, 1 red kurtha", 0.5)
            assert texts[-1] == funnels.THANKS  # the confirmation is written by code, whatever the model said
            lead = (await db.execute(select(SalesLead).where(SalesLead.customer_id == cust.id))).scalar_one()
            assert (lead.phone, lead.meta["order"]["items"]) == ("+9779801234567", "1 Red Kurtha")

            # 5. a menu over 3 photos: the model picks one page, the customer gets all of them in page order,
            # and the history gets one line for the lot
            fail["images"] = False
            db.add_all([
                ShopMedia(tenant_id=shop.id, source="upload", url=f"https://s/menu{n}.jpg", title=f"Menu page {n}",
                          tags=["menu", extra], created_at=ago(0.5 - n / 10))
                for n, extra in ((1, "breakfast"), (2, "momo"), (3, "drinks"))
            ])
            await db.flush()
            before = len(images)
            script.append("Yo hamro menu ho. [photo:P2]")
            await ask("menu pathaunus", 0.1)
            assert images[before:] == ["https://s/menu1.jpg", "https://s/menu2.jpg", "https://s/menu3.jpg"]
            notes = (await db.execute(select(Message.content).where(
                Message.conversation_id == convo.id, Message.message_type == "image",
                Message.media_url.like("https://s/menu%"), Message.content.isnot(None)))).scalars().all()
            assert notes == ["[sent 3 photos: Menu page 1, Menu page 2, Menu page 3]"]
            await db.rollback()

    asyncio.run(go())


def test_own_echo_does_not_pause_the_bot(monkeypatch) -> None:
    """Instagram echoes the bot's own messages, often before the send call returns. That echo must not be
    stored as a person replying (which pauses the bot): it takes the row stored before the send. An echo
    stored anyway (no row to match) is relabelled as the bot's. Unique ids; rolled back."""
    import uuid

    from app.api.v1.webhooks import ingest_event
    from app.models.shop_media import ShopMedia
    from app.services.meta import instagram

    run = uuid.uuid4().hex[:8]
    script: list[str] = []
    sent: list[str] = []

    async def go() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction

            def echo(mid, attachments=None):
                return {"sender": {"id": f"biz-{run}"}, "recipient": {"id": f"cust-{run}"},
                        "message": {"mid": mid, "is_echo": True, "text": None if attachments else "x",
                                    **({"attachments": attachments} if attachments else {})}}

            async def fake_text(token, acct, to, text):  # the echo arrives while the send call is in flight
                mid = f"mid-t-{run}-{len(sent)}"
                sent.append(text)
                await ingest_event(db, f"biz-{run}", echo(mid))
                return {"recipient_id": to, "message_id": mid}

            async def fake_image(token, acct, to, url):
                mid = f"mid-i-{run}-{len(sent)}"
                sent.append(url)
                await ingest_event(db, f"biz-{run}", echo(mid, [{"type": "image", "payload": {"url": "https://cdn.ig/e.jpg"}}]))
                return {"recipient_id": to, "message_id": mid}

            async def fake_generate(system, history, text, hint=""):
                return script.pop(0)

            async def no_profile(*_a, **_k):
                return {}

            monkeypatch.setattr(instagram, "send_text", fake_text)
            monkeypatch.setattr(instagram, "send_image", fake_image)
            monkeypatch.setattr(instagram, "fetch_customer_profile", no_profile)
            monkeypatch.setattr(reply_pipeline, "generate_reply", fake_generate)

            tenant = Tenant(name="Echo shop", ai_auto_reply=True)
            db.add(tenant)
            await db.flush()
            db.add(KnowledgeDocument(tenant_id=tenant.id, title="Shop", content="Chura Rs 300"))
            acct = SocialAccount(tenant_id=tenant.id, platform="instagram", external_account_id=f"biz-{run}",
                                 access_token_encrypted=encrypt_token("tok"))
            cust = Customer(tenant_id=tenant.id, external_user_id=f"cust-{run}", name="Sita")
            db.add_all([acct, cust, ShopMedia(tenant_id=tenant.id, source="upload", url="https://s/c.jpg",
                                              title="Chura", tags=["chura"])])
            await db.flush()
            convo = Conversation(tenant_id=tenant.id, customer_id=cust.id, social_account_id=acct.id, channel="instagram")
            db.add(convo)
            await db.flush()

            async def ask(text):
                m = Message(conversation_id=convo.id, sender_type="customer", content=text)
                db.add(m)
                await db.flush()
                await reply_pipeline.auto_reply(db, m.id)

            async def rows(sender):
                return (await db.execute(select(Message).where(
                    Message.conversation_id == convo.id, Message.sender_type == sender))).scalars().all()

            script.append("Chura Rs 300 ho. [photo:P1]")
            await ask("chura cha?")
            assert len(sent) == 2 and await rows("agent") == []
            assert sorted(m.external_message_id for m in await rows("ai")) == [f"mid-i-{run}-1", f"mid-t-{run}-0"]
            assert not await reply_pipeline.human_active(db, convo.id)

            # a photo set: photo 1's echo arrives late, after photo 2's row is waiting. It is already stored:
            # it must not take photo 2's row (that id is unique: the whole event failed)
            nxt = Message(conversation_id=convo.id, sender_type="ai", message_type="image", ai_generated=True)
            db.add(nxt)
            await db.flush()
            img = [{"type": "image", "payload": {"url": "https://cdn.ig/e.jpg"}}]
            assert await ingest_event(db, f"biz-{run}", echo(f"mid-i-{run}-1", img)) is None
            await db.refresh(nxt)
            assert nxt.external_message_id is None and await rows("agent") == []
            await db.delete(nxt)
            await db.flush()

            # the echo was stored as a person's message before the bot's row could match it: relabelled as ours
            async def stored_first(token, acct, to, text):
                db.add(Message(conversation_id=convo.id, sender_type="agent", content=text,
                               external_message_id=f"mid-late-{run}"))
                await db.flush()
                return {"message_id": f"mid-late-{run}"}

            monkeypatch.setattr(instagram, "send_text", stored_first)
            script.append("Aru kehi?")
            await ask("ok")
            assert await rows("agent") == [] and not await reply_pipeline.human_active(db, convo.id)
            assert [m.content for m in await rows("ai")].count("Aru kehi?") == 1
            await db.rollback()

    asyncio.run(go())


def test_dashboard_send_takes_over_its_echo(monkeypatch) -> None:
    """A person replies from the dashboard; Instagram's echo of it is stored first (it often beats the send call's
    answer, always for a photo). The dashboard must take that row over, not insert a second one under the same
    Meta id (which used to fail on the unique index after the customer already had the message). Rolled back."""
    import uuid

    from app.api.v1.conversations import reply, send_media
    from app.api.v1.webhooks import ingest_event
    from app.models.shop_media import ShopMedia
    from app.models.user import User
    from app.schemas.conversation import ReplyIn
    from app.schemas.shop_media import SendMediaIn
    from app.services.meta import instagram

    run = uuid.uuid4().hex[:8]

    async def go() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction

            async def echo_first(mid, attachments=None):
                event = {"sender": {"id": f"biz-{run}"}, "recipient": {"id": f"cust-{run}"},
                         "message": {"mid": mid, "is_echo": True, "text": None if attachments else "Namaste!",
                                     **({"attachments": attachments} if attachments else {})}}
                await ingest_event(db, f"biz-{run}", event)
                return {"recipient_id": f"cust-{run}", "message_id": mid}

            async def no_profile(*_a, **_k):
                return {}

            monkeypatch.setattr(instagram, "send_text", lambda *a: echo_first(f"mid-d-{run}"))
            monkeypatch.setattr(instagram, "send_image", lambda *a: echo_first(
                f"mid-p-{run}", [{"type": "image", "payload": {"url": "https://cdn.ig/echo.jpg"}}]))
            monkeypatch.setattr(instagram, "fetch_customer_profile", no_profile)

            tenant = Tenant(name="Desk shop")
            db.add(tenant)
            await db.flush()
            user = User(tenant_id=tenant.id, name="Asha", email=f"asha-{run}@x.np")
            acct = SocialAccount(tenant_id=tenant.id, platform="instagram", external_account_id=f"biz-{run}",
                                 access_token_encrypted=encrypt_token("tok"))
            cust = Customer(tenant_id=tenant.id, external_user_id=f"cust-{run}", name="Sita")
            photo = ShopMedia(tenant_id=tenant.id, source="upload", url="https://s/ours.jpg", title="Chura")
            db.add_all([user, acct, cust, photo])
            await db.flush()
            convo = Conversation(tenant_id=tenant.id, customer_id=cust.id, social_account_id=acct.id, channel="instagram")
            db.add(convo)
            await db.flush()

            text = await reply(convo.id, ReplyIn(text="Namaste!"), tenant, user, db)
            pic = await send_media(convo.id, SendMediaIn(media_id=photo.id), tenant, user, db)
            assert (text.external_message_id, text.sender_agent_id, text.sender_type) == (f"mid-d-{run}", user.id, "agent")
            assert (pic.external_message_id, pic.sender_agent_id, pic.media_url) == (f"mid-p-{run}", user.id, "https://s/ours.jpg")
            rows = (await db.execute(select(Message).where(Message.conversation_id == convo.id))).scalars().all()
            assert len(rows) == 2
            await db.rollback()

    asyncio.run(go())
