"""Outbound HTTP for the per-message hot path: customer profile lookups, the LLM call and message sends.

These run once per customer message, and httpx costs ~2ms of CPU per request (aiohttp ~0.7ms on a shared
session, measured on the load-test box). So they use one shared aiohttp session per process, opened at app
startup (main.lifespan). Rare calls (OAuth, subscribe, admin) stay on httpx.

request_json raises httpx's own exception types (HTTPStatusError with the response body, TransportError), so
the existing `except httpx.HTTPError` handlers around these calls, and the Meta error text they pass on to
the UI, keep working unchanged.

SSL_CTX: httpx builds a fresh SSL context (reloads the CA bundle, tens of ms of CPU) each time a client is
created, even for plain http:// URLs. With a client per call that blocked the event loop under load, so every
DB connection sat idle in its transaction and the pool ran dry. Pass `verify=SSL_CTX` instead.
"""
import asyncio
import json as jsonlib

import aiohttp
import httpx

SSL_CTX = httpx.create_ssl_context()

_session: aiohttp.ClientSession | None = None


def start() -> None:
    """Open the shared session (app startup, inside the running loop).
    ponytail: one connection pool for every host (100 total); split per host if a slow API starves the rest."""
    global _session
    _session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX, limit=100, ttl_dns_cache=300))


async def stop() -> None:
    global _session
    s, _session = _session, None
    if s is not None:
        await s.close()


async def request_json(
    method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
    json: dict | None = None, timeout: float = 15.0,
) -> dict:
    """One JSON request. Raises httpx.HTTPStatusError on a non-2xx answer (body kept on .response) and
    httpx.TransportError on a connection failure or timeout. Messages carry the URL without its query string."""
    shown = url.split("?", 1)[0]
    session = _session or aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX))  # tests, scripts
    try:
        async with session.request(
            method, url, params=params, headers=headers, json=json, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            text = await resp.text()
            status = resp.status
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise httpx.TransportError(f"{method} {shown}: {exc!r}") from exc
    finally:
        if session is not _session:
            await session.close()
    if status >= 400:
        req = httpx.Request(method, shown)
        raise httpx.HTTPStatusError(
            f"{status} from {method} {shown}", request=req, response=httpx.Response(status, text=text, request=req)
        )
    return jsonlib.loads(text) if text else {}
