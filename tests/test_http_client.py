"""http_client.request_json against a real local server: JSON in/out, and failures arrive as httpx errors
(so the `except httpx.HTTPError` handlers around sends and lookups keep working)."""
import asyncio

import httpx
from aiohttp import web

from app.services import http_client


async def _echo(request: web.Request) -> web.Response:
    return web.json_response({"auth": request.headers.get("Authorization"), "q": dict(request.query),
                              "body": await request.json() if request.can_read_body else None})


async def _refuse(request: web.Request) -> web.Response:
    return web.json_response({"error": {"message": "outside the 24h window"}}, status=400)


async def _slow(request: web.Request) -> web.Response:
    await asyncio.sleep(1)
    return web.json_response({})


async def _run() -> None:
    app = web.Application()
    app.add_routes([web.post("/echo", _echo), web.post("/refuse", _refuse), web.post("/slow", _slow)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"

    for shared in (False, True):  # no session started (tests, scripts) and the app's shared session
        if shared:
            http_client.start()
        got = await http_client.request_json(
            "POST", base + "/echo", params={"fields": "name"}, headers={"Authorization": "Bearer T"}, json={"a": 1}
        )
        assert got == {"auth": "Bearer T", "q": {"fields": "name"}, "body": {"a": 1}}

        try:
            await http_client.request_json("POST", base + "/refuse?access_token=SECRET", json={})
            raise AssertionError("a 400 must raise")
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 400 and "24h window" in exc.response.text
            assert "SECRET" not in str(exc) and "SECRET" not in str(exc.request.url)  # never quote the query string

        try:
            await http_client.request_json("POST", base + "/slow", json={}, timeout=0.2)
            raise AssertionError("a timeout must raise")
        except httpx.TransportError:
            pass
        if shared:
            await http_client.stop()

    await runner.cleanup()  # server gone: connection refused
    try:
        await http_client.request_json("POST", base + "/echo", json={})
        raise AssertionError("a refused connection must raise")
    except httpx.HTTPError:
        pass


def test_request_json():
    asyncio.run(_run())
