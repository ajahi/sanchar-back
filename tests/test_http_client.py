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


def test_untrusted_certificate_is_refused():
    """TLS verification is on: a server whose certificate no trusted CA signed must raise, not be talked to."""
    import datetime
    import ssl
    import tempfile
    from pathlib import Path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )

    async def run() -> None:
        with tempfile.TemporaryDirectory() as d:
            crt, k = Path(d, "c.pem"), Path(d, "k.pem")
            crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                            serialization.NoEncryption()))
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(crt, k)
            app = web.Application()
            app.add_routes([web.post("/echo", _echo)])
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx).start()
            try:
                await http_client.request_json("POST", f"https://127.0.0.1:{runner.addresses[0][1]}/echo", json={})
                raise AssertionError("an untrusted certificate must be refused")
            except httpx.TransportError:
                pass
            finally:
                await runner.cleanup()

    asyncio.run(run())


def test_proxy_environment_is_honoured(monkeypatch):
    """httpx used HTTP(S)_PROXY / NO_PROXY; so must the aiohttp path, or a proxied deployment is bypassed."""
    async def run() -> None:
        app = web.Application()
        app.add_routes([web.post("/echo", _echo)])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/echo"
        try:
            monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")  # nothing listens there
            monkeypatch.delenv("NO_PROXY", raising=False)
            monkeypatch.delenv("no_proxy", raising=False)
            try:
                await http_client.request_json("POST", url, json={})
                raise AssertionError("the call must have gone through the (dead) proxy")
            except httpx.TransportError:
                pass
            monkeypatch.setenv("NO_PROXY", "127.0.0.1")
            assert (await http_client.request_json("POST", url, json={"a": 1}))["body"] == {"a": 1}
        finally:
            await runner.cleanup()

    asyncio.run(run())
