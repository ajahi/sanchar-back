"""A DB-pool timeout during ingest must answer 503 so Meta redelivers; any other ingest error keeps its 200."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.api.v1 import webhooks
from app.db.session import get_db
from app.main import app
from app.services.meta import instagram


class FakeDb:
    async def rollback(self) -> None:
        pass


async def fake_db():
    yield FakeDb()


@pytest.mark.parametrize("error, expected", [(PoolTimeout("pool"), 503), (ValueError("bad event"), 200)])
def test_ingest_failure_status(monkeypatch, error, expected) -> None:
    async def failing_ingest(*_a):
        raise error

    monkeypatch.setattr(webhooks, "ingest_event", failing_ingest)
    monkeypatch.setattr(instagram, "verify_webhook_signature", lambda *_a: True)
    app.dependency_overrides[get_db] = fake_db
    try:
        body = {"object": "instagram", "entry": [{"id": "1", "messaging": [{}]}]}
        assert TestClient(app).post("/api/v1/webhooks/instagram", json=body).status_code == expected
    finally:
        app.dependency_overrides.pop(get_db)
