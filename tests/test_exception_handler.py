"""An unhandled error must come back as the handler's JSON 500, not crash the error middleware."""
from fastapi.testclient import TestClient

from app.main import app


def test_unhandled_error_is_json_500():
    async def boom():
        raise RuntimeError("boom")

    app.add_api_route("/__boom", boom)
    try:
        res = TestClient(app, raise_server_exceptions=False).get("/__boom")
    finally:
        app.router.routes.pop()
    assert res.status_code == 500 and "detail" in res.json()
