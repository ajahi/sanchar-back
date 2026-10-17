from app.api.v1.shop_media import _sniff


def test_sniff_accepts_real_images_only() -> None:
    assert _sniff(b"\xff\xd8\xff\xe0rest") == ("jpg", "image/jpeg")
    assert _sniff(b"\x89PNG\r\n\x1a\nrest") == ("png", "image/png")
    assert _sniff(b"RIFF\x00\x00\x00\x00WEBPrest") == ("webp", "image/webp")
    assert _sniff(b"GIF89a") is None  # not allowed
    assert _sniff(b"<svg onload=alert(1)>") is None  # an extension or content-type claim can't smuggle this in


def test_post_to_instagram(monkeypatch) -> None:
    """Post an uploaded photo: post id + link kept; the next Instagram sync matches it without
    replacing our permanent url. Runs in a rolled-back transaction."""
    import asyncio

    import pytest
    from fastapi import HTTPException

    from app.api.v1 import shop_media as api
    from app.core.security import encrypt_token
    from app.db.session import async_session_factory
    from app.models.shop_media import ShopMedia
    from app.models.social_account import SocialAccount
    from app.models.tenant import Tenant
    from app.schemas.shop_media import PostIn

    posted: list[tuple] = []

    async def fake_publish(token, account_id, url, caption):
        posted.append((account_id, url, caption))
        return {"id": "17890001", "permalink": "https://www.instagram.com/p/abc/"}

    async def fake_fetch(token, limit):  # Instagram now lists that same post, with an expiring CDN url
        return [{"id": "17890001", "media_type": "IMAGE", "media_url": "https://cdn.ig/expiring.jpg",
                 "caption": "New sharara!", "permalink": "https://www.instagram.com/p/abc/"}]

    monkeypatch.setattr(api.instagram, "publish_image", fake_publish)
    monkeypatch.setattr(api.instagram, "fetch_media", fake_fetch)

    async def run() -> None:
        async with async_session_factory() as db:
            db.commit = db.flush  # stay inside the rolled-back transaction
            t = Tenant(name="Post T")
            db.add(t)
            await db.flush()
            photo = ShopMedia(tenant_id=t.id, source="upload", url="https://storage/ours.jpg")
            imported = ShopMedia(tenant_id=t.id, source="instagram", ig_media_id="old", url="https://cdn.ig/old.jpg")
            db.add_all([photo, imported])
            await db.flush()

            with pytest.raises(HTTPException) as e:  # no Instagram account connected
                await api.post_to_instagram(photo.id, PostIn(caption="x"), t, db)
            assert e.value.status_code == 409

            db.add(SocialAccount(tenant_id=t.id, platform="instagram", external_account_id="ig-1",
                                 access_token_encrypted=encrypt_token("tok")))
            await db.flush()
            with pytest.raises(HTTPException) as e:  # only our uploads can be posted
                await api.post_to_instagram(imported.id, PostIn(), t, db)
            assert e.value.status_code == 422

            out = await api.post_to_instagram(photo.id, PostIn(caption="  New sharara!  "), t, db)
            assert posted == [("ig-1", "https://storage/ours.jpg", "New sharara!")]
            assert (out.ig_media_id, out.permalink, out.caption) == ("17890001", "https://www.instagram.com/p/abc/", "New sharara!")
            with pytest.raises(HTTPException) as e:  # not twice
                await api.post_to_instagram(photo.id, PostIn(), t, db)
            assert e.value.status_code == 409

            api._last_sync.pop(t.id, None)
            await api._sync_instagram(db, t.id)
            await db.refresh(photo)
            assert photo.url == "https://storage/ours.jpg" and photo.source == "upload"  # matched, url kept
            rows = (await db.execute(api.select(ShopMedia).where(ShopMedia.tenant_id == t.id))).scalars().all()
            assert len(rows) == 2  # no duplicate row for the post
            await db.rollback()

    asyncio.run(run())
