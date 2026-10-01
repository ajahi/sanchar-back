from app.api.v1.shop_media import _sniff


def test_sniff_accepts_real_images_only() -> None:
    assert _sniff(b"\xff\xd8\xff\xe0rest") == ("jpg", "image/jpeg")
    assert _sniff(b"\x89PNG\r\n\x1a\nrest") == ("png", "image/png")
    assert _sniff(b"RIFF\x00\x00\x00\x00WEBPrest") == ("webp", "image/webp")
    assert _sniff(b"GIF89a") is None  # not allowed
    assert _sniff(b"<svg onload=alert(1)>") is None  # an extension or content-type claim can't smuggle this in
