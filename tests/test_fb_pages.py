"""Facebook Login's Page list (services/meta/facebook.fetch_user_pages), with Meta faked (httpx.MockTransport).

    python -m pytest tests/test_fb_pages.py
"""
import asyncio

import httpx

from app.services.meta import facebook


def test_page_left_out_of_the_list_is_fetched_by_id(monkeypatch):
    """A Page reached through a Business portfolio is missing from /me/accounts but granted in the popup:
    it is found through the grant and asked for by id. A Page already listed is not fetched twice."""
    asked: list[str] = []

    def meta(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        asked.append(path)
        if path == "accounts":
            return httpx.Response(200, json={"data": [{"id": "p1", "name": "Listed", "access_token": "t1"}]})
        if path == "debug_token":
            return httpx.Response(200, json={"data": {"granular_scopes": [
                {"scope": "pages_messaging", "target_ids": ["p9"]},
                {"scope": "pages_show_list", "target_ids": ["p1", "cafe"]},
            ]}})
        if path == "cafe":
            return httpx.Response(200, json={"id": "cafe", "name": "The Cube Cafe", "access_token": "t2"})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(facebook.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(meta), **kw))
    pages = asyncio.run(facebook.fetch_user_pages("user-token"))
    assert [(p["id"], p["access_token"]) for p in pages] == [("p1", "t1"), ("cafe", "t2")]
    assert asked == ["accounts", "debug_token", "cafe"]
