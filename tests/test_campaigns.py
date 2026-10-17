"""Campaigns: referral parsing, price/colour/photo intent, the fixed message, and the full flow
(ad opens a chat -> unmapped ad registers -> staff map it -> "pp" gets the price line + photos once).
The DB test runs in a transaction that is always rolled back.
    python -m pytest tests/test_campaigns.py
"""
import asyncio

from sqlalchemy import select

from app.api.v1.webhooks import ingest_event
from app.core.security import encrypt_token
from app.db.session import async_session_factory
from app.models.campaign import Campaign, CampaignLink
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.notification import Notification
from app.models.shop_media import ShopMedia
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.services import campaigns, reply_pipeline
from app.services.meta import instagram


def test_referral_of() -> None:
    ad = {"ad_id": "123", "source": "ADS", "type": "OPEN_THREAD",
          "ads_context_data": {"ad_title": "Chinnon sharara", "photo_url": "http://x/a.jpg"}}
    want = {"kind": "ad", "external_id": "123", "title": "Chinnon sharara", "image": "http://x/a.jpg"}
    assert campaigns.referral_of({"referral": ad}) == want  # on the event (messaging_referral)
    assert campaigns.referral_of({"message": {"mid": "m", "referral": ad}}) == want  # on the message
    assert campaigns.referral_of({"postback": {"referral": ad}}) == want
    assert campaigns.referral_of({"referral": {"ref": "SHARARA-01", "source": "SHORTLINKS"}}) == {
        "kind": "ref", "external_id": "SHARARA-01", "title": None, "image": None}
    wa = {"source_id": "999", "source_type": "ad", "headline": "Sale", "image_url": "http://x/w.jpg"}
    assert campaigns.referral_of({"referral": wa}) == {"kind": "ad", "external_id": "999", "title": "Sale", "image": "http://x/w.jpg"}
    assert campaigns.referral_of({"referral": {"source_id": "5", "source_type": "something"}}) is None
    assert campaigns.referral_of({"message": {"mid": "m", "text": "hi"}}) is None
    assert campaigns.referral_of({"referral": "garbage"}) is None


def test_wants_campaign_info() -> None:
    for text in ("pp", "Pp", "PP?", "price", "price kati ho?", "kati ho", "rate k ho", "colour?", "aru color cha?",
                 "photo pathau", "dekhau", "मूल्य कति हो", "रंग"):
        assert campaigns.wants_campaign_info(text), text
    for text in ("hello", "thanks", "app", "ppl", "", None, "   ",
                 "what is the price of delivery to Pokhara for two sets?"):  # long -> the AI answers
        assert not campaigns.wants_campaign_info(text), text


def test_campaign_text() -> None:
    c = Campaign(name="Sharara", price=3350, blurb="pure chinnon silk fabric, comes with quality guarantee")
    assert campaigns.campaign_text(c) == "Rs 3350 price, pure chinnon silk fabric, comes with quality guarantee"
    assert campaigns.campaign_text(Campaign(name="x", price=500, blurb=None)) == "Rs 500 price"
    assert campaigns.campaign_text(Campaign(name="x", price=None, blurb="  silk  ")) == "silk"
    assert campaigns.campaign_text(Campaign(name="x", price=None, blurb=None)) == ""
    assert campaigns.campaign_context(c).startswith("Sharara. Rs 3350 price")


async def _run(monkeypatch) -> None:
    async def no_profile(*_a, **_k):
        return {}

    sent: list[tuple] = []

    async def fake_text(token, acct, to, text):
        sent.append(("text", text))
        return {"message_id": f"t{len(sent)}"}

    async def fake_image(token, acct, to, url):
        sent.append(("image", url))
        return {"message_id": f"i{len(sent)}"}

    monkeypatch.setattr(instagram, "fetch_customer_profile", no_profile)
    monkeypatch.setattr(instagram, "send_text", fake_text)
    monkeypatch.setattr(instagram, "send_image", fake_image)

    async with async_session_factory() as db:
        t = Tenant(name="Camp T", ai_auto_reply=True)
        other = Tenant(name="Other T")
        db.add_all([t, other])
        await db.flush()
        acc = SocialAccount(tenant_id=t.id, platform="instagram", external_account_id="biz-c",
                            access_token_encrypted=encrypt_token("tok"))
        db.add(acc)
        await db.flush()

        # 1. a chat opens from an unknown ad (referral-only event, no message): link + notification, nothing stored
        ev = {"sender": {"id": "cust-c"}, "recipient": {"id": "biz-c"}, "timestamp": 1700000000000,
              "referral": {"ad_id": "AD1", "ads_context_data": {"ad_title": "Sharara ad"}}}
        assert await ingest_event(db, "biz-c", ev) is None
        link = (await db.execute(select(CampaignLink).where(CampaignLink.tenant_id == t.id))).scalar_one()
        assert (link.kind, link.external_id, link.ad_title, link.campaign_id) == ("ad", "AD1", "Sharara ad", None)
        note = (await db.execute(select(Notification).where(Notification.event == "ad_needs_campaign",
                                                            Notification.tenant_id == t.id))).scalar_one()
        assert "Sharara ad" in note.subject
        convo = (await db.execute(select(Conversation).where(Conversation.tenant_id == t.id))).scalar_one()
        assert convo.campaign_link_id == link.id
        assert await campaigns.campaign_for(db, convo) is None  # unmapped: no campaign reply yet

        # a second referral for the same ad does not duplicate the link or the notification
        await ingest_event(db, "biz-c", ev)
        assert len((await db.execute(select(CampaignLink).where(CampaignLink.tenant_id == t.id))).scalars().all()) == 1

        # 2. staff create the campaign, attach photos (one sold out), map the ad
        camp = Campaign(tenant_id=t.id, name="Sharara", price=3350, blurb="pure chinnon silk")
        db.add(camp)
        await db.flush()
        for i, (stock, order) in enumerate([(True, 2), (False, 1), (True, 0)]):
            db.add(ShopMedia(tenant_id=t.id, source="upload", url=f"http://x/{i}.jpg", in_stock=stock,
                             campaign_id=camp.id, sort_order=order))
        db.add(ShopMedia(tenant_id=other.id, source="upload", url="http://x/other.jpg"))  # not this campaign
        link.campaign_id = camp.id
        await db.flush()
        assert (await campaigns.campaign_for(db, convo)).id == camp.id

        # 3. the customer asks "pp": price line first, then the in-stock photos in order (sold out skipped)
        first = await ingest_event(db, "biz-c", {"sender": {"id": "cust-c"}, "recipient": {"id": "biz-c"},
                                                  "timestamp": 1700000001000, "message": {"mid": "m1", "text": "Pp"}})
        assert first is not None
        await db.commit()  # the reply pipeline commits its own work; keep this test's rows visible to it
        try:
            await reply_pipeline.auto_reply(db, first[1])
            assert sent == [("text", "Rs 3350 price, pure chinnon silk"), ("image", "http://x/2.jpg"), ("image", "http://x/0.jpg")]

            # asking again soon: the price line again, but the photos are not resent into the same chat
            sent.clear()
            again = await ingest_event(db, "biz-c", {"sender": {"id": "cust-c"}, "recipient": {"id": "biz-c"},
                                                      "timestamp": 1700000002000, "message": {"mid": "m2", "text": "price kati ho"}})
            await db.commit()
            await reply_pipeline.auto_reply(db, again[1])
            assert sent == [("text", "Rs 3350 price, pure chinnon silk")]

            stored = (await db.execute(select(Message.message_type, Message.sender_type)
                                       .where(Message.conversation_id == convo.id)
                                       .order_by(Message.created_at))).all()
            assert [r for r in stored if r.sender_type == "ai" and r.message_type == "image"] and len(stored) >= 6
        finally:  # committed above: remove both tenants (everything under them cascades)
            for tid in (t.id, other.id):
                tenant = await db.get(Tenant, tid)
                if tenant is not None:
                    await db.delete(tenant)
            await db.commit()


def test_campaign_flow(monkeypatch) -> None:
    asyncio.run(_run(monkeypatch))


async def _publish(monkeypatch) -> None:
    import pytest
    from fastapi import HTTPException

    from app.api.v1 import campaigns as api
    from app.schemas.campaign import PublishIn

    posted: list[tuple] = []

    async def fake_publish(token, account_id, url, caption):
        posted.append((account_id, url, caption))
        return {"id": "post-1", "permalink": "https://instagram.com/p/x"}

    monkeypatch.setattr(api.instagram, "publish_image", fake_publish)
    async with async_session_factory() as db:
        db.commit = db.flush  # stay inside the rolled-back transaction
        t = Tenant(name="Pub T")
        db.add(t)
        await db.flush()
        camp = Campaign(tenant_id=t.id, name="Sharara", price=3350, details="Colours: maroon, navy")
        photo = ShopMedia(tenant_id=t.id, source="upload", url="https://cdn/x.jpg")
        ig_photo = ShopMedia(tenant_id=t.id, source="instagram", url="https://ig-cdn/y.jpg")
        db.add_all([camp, photo, ig_photo])
        await db.flush()

        # no Instagram account yet -> refused
        with pytest.raises(HTTPException) as e:
            await api.publish_post(camp.id, PublishIn(media_id=photo.id, caption="hi"), t, db)
        assert e.value.status_code == 409

        db.add(SocialAccount(tenant_id=t.id, platform="instagram", external_account_id="ig-pub",
                             access_token_encrypted=encrypt_token("tok")))
        await db.flush()
        # only photos on our storage can be posted (Instagram downloads them)
        with pytest.raises(HTTPException) as e:
            await api.publish_post(camp.id, PublishIn(media_id=ig_photo.id), t, db)
        assert e.value.status_code == 422

        out = await api.publish_post(camp.id, PublishIn(media_id=photo.id, caption="  New sharara!  "), t, db)
        assert posted == [("ig-pub", "https://cdn/x.jpg", "New sharara!")]
        assert (out.ig_post_id, out.ig_permalink, out.details) == ("post-1", "https://instagram.com/p/x", "Colours: maroon, navy")
        assert photo.campaign_id == camp.id  # the posted photo joins the campaign
        assert campaigns.campaign_context(camp).endswith("\nColours: maroon, navy")  # details reach the AI
        await db.rollback()


def test_publish_post(monkeypatch) -> None:
    asyncio.run(_publish(monkeypatch))
