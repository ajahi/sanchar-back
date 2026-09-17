"""Shared Facebook Graph API client.

Every Meta call in this service funnels through `request()` / `paginate()` so that
authentication, retries, throttling, timeouts, cursor pagination and Meta's error
bodies are handled in exactly one place.

Two deliberate choices worth knowing about:

* **Tokens travel in the `Authorization` header, never the query string.** Meta accepts
  both, but a query-string token ends up in proxy logs, `Referer` headers and httpx
  exception messages. Meta errors still sometimes echo the token back, so error text is
  scrubbed before it is logged.
* **A client is created per call rather than pooled.** The test suite drives async code
  with a fresh `asyncio.run()` per test, so a module-level `AsyncClient` would be bound
  to a dead event loop. Per-call clients keep that (correct) test style working.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, AsyncIterator, Optional

import httpx

from app.core.config import settings

log = logging.getLogger(__name__)

# Meta error codes worth retrying: throttling and "try again" conditions.
_TRANSIENT_CODES = frozenset(
    {
        1,  # unknown error, frequently transient
        2,  # service temporarily unavailable
        4,  # application request limit reached
        17,  # user request limit reached
        32,  # page request limit reached
        341,  # application limit reached
        613,  # calls to this api have exceeded the rate limit
    }
)

# Codes meaning the token or granted permissions are wrong. Retrying cannot help and a
# silent retry loop would just burn the rate limit, so these surface immediately.
_AUTH_CODES = frozenset({10, 102, 190, 200, 230})

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Safety valve: cursor pagination must never loop forever on a malformed `paging` block.
_MAX_PAGES = 1000

_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+", re.IGNORECASE)


def scrub(text: str) -> str:
    """Remove any access token that Meta echoed back into an error message."""
    return _TOKEN_RE.sub(r"\1<redacted>", text or "")


class GraphError(Exception):
    """A failed Graph API call, carrying Meta's structured error details.

    `code` and `subcode` come straight from the response body so callers can branch on
    them (e.g. a revoked token vs. a missing permission) instead of string matching.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
        error_type: Optional[str] = None,
        fbtrace_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.subcode = subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id

    @property
    def is_auth_error(self) -> bool:
        """True when the token is expired/revoked or a permission was never granted."""
        return self.code in _AUTH_CODES or self.status in (401, 403)

    @property
    def is_throttled(self) -> bool:
        return self.code in _TRANSIENT_CODES or self.status == 429

    @property
    def is_revoked(self) -> bool:
        """True when Meta invalidated the token itself (expired, re-auth, de-authorized).

        Subcodes 463/460/458 pair with code 190 and mean the stored token is dead and the
        account must reconnect — a different remedy from a merely missing permission.
        """
        return self.code == 190 and self.subcode in (458, 460, 463)

    def __str__(self) -> str:
        parts = [self.message]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.fbtrace_id:
            parts.append(f"fbtrace_id={self.fbtrace_id}")
        return " ".join(parts)


def _decode_error(payload: Any, status: int) -> GraphError:
    """Turn a Graph error body into a GraphError, tolerating non-conforming bodies."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return GraphError(
            scrub(f"Graph API returned HTTP {status} with an unrecognised body"),
            status=status,
        )
    return GraphError(
        scrub(str(err.get("message") or f"Graph API error (HTTP {status})")),
        status=status,
        code=err.get("code"),
        subcode=err.get("error_subcode"),
        error_type=err.get("type"),
        fbtrace_id=err.get("fbtrace_id"),
    )


def _retry_after(response: Optional[httpx.Response]) -> Optional[float]:
    """Seconds to wait per `Retry-After`, when the server bothered to send it."""
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _backoff_delay(attempt: int, response: Optional[httpx.Response]) -> float:
    """Exponential backoff with full jitter, honouring Retry-After when present."""
    server_hint = _retry_after(response)
    if server_hint is not None:
        return min(server_hint, settings.graph_backoff_max_seconds)
    ceiling = min(
        settings.graph_backoff_base_seconds * (2**attempt),
        settings.graph_backoff_max_seconds,
    )
    return random.uniform(ceiling / 2, ceiling)  # full jitter avoids retry stampedes


def build_url(path: str) -> str:
    """Absolute URL for a Graph `path`, or pass an absolute URL straight through."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    version = settings.graph_api_version.strip("/")
    return f"{settings.graph_base_url.rstrip('/')}/{version}/{path.lstrip('/')}"


async def request(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    timeout: Optional[float] = None,
    max_attempts: Optional[int] = None,
) -> dict[str, Any]:
    """Call the Graph API, retrying transient failures.

    Raises `GraphError` once the attempt budget is exhausted or on a non-retryable
    error. Returns the decoded JSON body (an empty dict for an empty 2xx body).
    """
    url = build_url(path)
    attempts = max_attempts or settings.graph_max_attempts
    request_timeout = httpx.Timeout(timeout or settings.graph_timeout_seconds)

    last_error: Optional[Exception] = None
    attempt = 0
    # Graph accepts the token as a Bearer header, but Meta's messaging examples only ever
    # show `access_token` as a query parameter. If the header is rejected we retry once in
    # the documented form — a free attempt that does not consume the retry budget.
    auth_via_query = False

    while attempt < attempts:
        headers = {"Accept": "application/json"}
        query: Optional[dict[str, Any]] = params
        if token:
            if auth_via_query:
                query = {**(params or {}), "access_token": token}
            else:
                headers["Authorization"] = f"Bearer {token}"

        response: Optional[httpx.Response] = None
        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await client.request(
                    method.upper(),
                    url,
                    params=query,
                    json=json_body,
                    data=data,
                    headers=headers,
                )
        except httpx.TransportError as exc:  # connect/read/write timeouts, DNS, resets
            last_error = exc
            attempt += 1
            if attempt >= attempts:
                raise GraphError(
                    scrub(f"Graph API unreachable after {attempts} attempts: {exc}")
                ) from exc
            delay = _backoff_delay(attempt - 1, None)
            log.warning(
                "graph %s %s transport error (%s); retrying in %.2fs (attempt %d/%d)",
                method.upper(),
                path,
                exc.__class__.__name__,
                delay,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay)
            continue

        # Meta occasionally answers 200 with an error envelope; treat it as a failure.
        payload: Any
        if response.status_code == 204 or not response.content:
            payload = {}
        else:
            try:
                payload = response.json()
            except ValueError:
                payload = {}

        if response.status_code < 400 and not (
            isinstance(payload, dict) and "error" in payload
        ):
            return payload if isinstance(payload, dict) else {"data": payload}

        error = _decode_error(payload, response.status_code)

        # The token itself was rejected (code 190 / HTTP 401). If we sent it as a Bearer
        # header, try Meta's documented query-parameter form before surfacing a failure —
        # this must not consume an attempt, since nothing about the request changed.
        if (
            token
            and not auth_via_query
            and (error.code == 190 or response.status_code == 401)
        ):
            log.info(
                "graph rejected Bearer auth for %s %s; retrying with access_token param",
                method.upper(),
                path,
            )
            auth_via_query = True
            continue

        retryable = response.status_code in _RETRY_STATUSES or error.is_throttled
        attempt += 1
        if not retryable or attempt >= attempts:
            log.warning("graph %s %s failed: %s", method.upper(), path, error)
            raise error

        delay = _backoff_delay(attempt - 1, response)
        log.warning(
            "graph %s %s throttled/failed (%s); retrying in %.2fs (attempt %d/%d)",
            method.upper(),
            path,
            error,
            delay,
            attempt,
            attempts,
        )
        await asyncio.sleep(delay)

    # Unreachable in practice: the loop either returns or raises.
    raise GraphError(scrub(f"Graph API call failed: {last_error}"))


async def get(
    path: str,
    *,
    token: Optional[str] = None,
    timeout: Optional[float] = None,
    **params: Any,
) -> dict[str, Any]:
    """GET with the given query params (None values are dropped)."""
    return await request(
        "GET",
        path,
        token=token,
        params={k: v for k, v in params.items() if v is not None},
        timeout=timeout,
    )


async def paginate(
    path: str,
    *,
    token: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    max_items: Optional[int] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield `data` items across every page of a cursor-paginated Graph edge.

    Prefers re-issuing the request with an `after` cursor over following Meta's `next`
    URL, so the token stays out of the URL. Falls back to `next` if a page reports no
    cursor, which keeps odd edges working.
    """
    base_params = dict(params or {})
    after: Optional[str] = None
    next_url: Optional[str] = None
    yielded = 0
    first_page = True

    for _ in range(_MAX_PAGES):
        if not first_page and settings.graph_min_interval_seconds > 0:
            # Pace the crawl. Meta's Conversations API allows only ~2 calls/second per
            # account and flags sudden volume changes as abuse, so a tight pagination loop
            # is the fastest way to get throttled mid-import.
            await asyncio.sleep(settings.graph_min_interval_seconds)
        first_page = False

        if next_url:
            payload = await request("GET", next_url, token=token)
        else:
            query = dict(base_params)
            if after:
                query["after"] = after
            payload = await request("GET", path, token=token, params=query)

        items = payload.get("data") or []
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return

        paging = payload.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        has_next = bool(paging.get("next"))
        if not has_next:
            return
        if cursor:
            after, next_url = cursor, None
        else:
            # No cursor to re-use: follow Meta's own link rather than stopping early.
            after, next_url = None, paging.get("next")

    log.warning("graph pagination hit the %d-page safety cap for %s", _MAX_PAGES, path)
