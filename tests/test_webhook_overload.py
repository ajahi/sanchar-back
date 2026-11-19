"""Any ingest failure, a DB-pool timeout included, answers 200: a 5xx risks Meta disabling the webhook."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.api.v1 import webhooks
from app.db.session import get_db
from app.main import app
from app.services import reply_queue
from app.services.meta import instagram


class FakeDb:
    async def rollback(self) -> None:
        pass


async def fake_db():
    yield FakeDb()


@pytest.mark.parametrize("error, expected", [(PoolTimeout("pool"), 200), (ValueError("bad event"), 200)])
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


def test_queued_body_is_not_ingested_by_the_api(monkeypatch) -> None:
    """A broker-confirmed body is acked unstored: the worker ingests it, so the API touches no DB connection."""
    async def must_not_run(*_a):
        raise AssertionError("ingested inline")

    async def confirmed(raw):
        return True

    monkeypatch.setattr(webhooks, "ingest_event", must_not_run)
    monkeypatch.setattr(reply_queue, "publish_webhook", confirmed)
    monkeypatch.setattr(instagram, "verify_webhook_signature", lambda *_a: True)
    app.dependency_overrides[get_db] = fake_db
    try:
        body = {"object": "instagram", "entry": [{"id": "1", "messaging": [{}]}]}
        assert TestClient(app).post("/api/v1/webhooks/instagram", json=body).status_code == 200
    finally:
        app.dependency_overrides.pop(get_db)
