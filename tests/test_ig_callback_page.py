"""The callback's preloader page must not let a crafted code/state escape its JS string.

    python -m pytest tests/test_ig_callback_page.py
"""
import asyncio

from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.v1.social_accounts import instagram_callback


def test_callback_page() -> None:
    evil = '");alert(1)//</script><script>'
    resp = asyncio.run(instagram_callback(code=evil, state="s.t-u_v"))
    assert isinstance(resp, HTMLResponse)
    body = resp.body.decode()
    assert 'location.replace("finish?code=' in body
    assert "alert(1)" not in body and body.count("<script>") == 1

    denied = asyncio.run(instagram_callback(error="access_denied"))
    assert isinstance(denied, RedirectResponse) and "ig_error=access_denied" in denied.headers["location"]
