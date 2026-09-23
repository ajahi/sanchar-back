"""Graph attachment parsing + webhook signature accepting either app secret.

    python -m pytest tests/test_ig_media.py
"""
import hmac
from hashlib import sha256

from app.api.v1.webhooks import graph_attachment
from app.core.config import settings
from app.services.meta import instagram


def test_graph_attachment() -> None:
    img = {"attachments": {"data": [{"image_data": {"url": "https://x/i.jpg"}}]}}
    vid = {"attachments": {"data": [{"video_data": {"url": "https://x/v.mp4"}}]}}
    aud = {"attachments": {"data": [{"file_url": "https://x/a.mp4", "mime_type": "audio/mpeg"}]}}
    assert graph_attachment(img) == ("image", "https://x/i.jpg")
    assert graph_attachment(vid) == ("video", "https://x/v.mp4")
    assert graph_attachment(aud) == ("audio", "https://x/a.mp4")
    assert graph_attachment({"attachments": {"data": [{"file_url": "https://x/f"}]}}) == ("file", "https://x/f")
    assert graph_attachment({"message": "hi"}) == ("text", None)
    assert graph_attachment({"attachments": {"data": []}}) == ("text", None)


def test_signature_either_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "instagram_app_secret", "ig-secret")
    monkeypatch.setattr(settings, "meta_app_secret", "meta-secret")
    body = b'{"object":"instagram"}'
    sign = lambda s: "sha256=" + hmac.new(s.encode(), body, sha256).hexdigest()  # noqa: E731
    assert instagram.verify_webhook_signature(body, sign("ig-secret"))
    assert instagram.verify_webhook_signature(body, sign("meta-secret"))
    assert not instagram.verify_webhook_signature(body, sign("wrong"))
    assert not instagram.verify_webhook_signature(body, "")
