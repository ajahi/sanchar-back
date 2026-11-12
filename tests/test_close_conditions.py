"""The owner's "we are closed" switch (tenants.close_conditions): on, off, ending by itself, its message, the endpoints, and
replies asking for order details while closed. Endpoint parts run against Postgres, rolled back.

    python -m pytest tests/test_close_conditions.py
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api.v1.tenants import close_shop, get_close_conditions, reopen_shop
from app.db.session import async_session_factory
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant import CloseConditionsIn
from app.services import hours
from app.services.funnels import asks_order_details
from app.services.hours import NEPAL

NOON = datetime(2026, 11, 12, 12, 0, tzinfo=NEPAL)


def test_close_conditions_message():
    assert hours.close_conditions_message(None) is None and hours.close_conditions_message({}) is None
    assert hours.close_conditions_message({"message": "Aaja banda chau, bholi aaunus.", "until": None}, NOON) == "Aaja banda chau, bholi aaunus."
    assert hours.close_conditions_message({"message": "  ", "until": None}, NOON) == hours.DEFAULT_CLOSED_MESSAGE  # no message written
    tonight = hours.end_of_today(NOON)
    assert tonight == datetime(2026, 11, 13, 0, 0, tzinfo=NEPAL)
    on = {"message": "Banda", "until": tonight.isoformat()}
    assert hours.close_conditions_message(on, NOON) == "Banda"
    assert hours.close_conditions_message(on, tonight) is None  # ended by itself at midnight
    assert hours.close_conditions_message(on, NOON + timedelta(days=1)) is None


def test_order_detail_questions_are_caught():
    for asking in (
        "Sure! Delivery ki pickup?",
        "Dry Mix ko kun type chahanu huncha? Delivery ki pickup?",
        "Please tell me your address and phone number.",
        "Tapai ko naam ra phone number dinus na.",
        "Kati plate chahiyo?",
    ):
        assert asks_order_details(asking), asking
    for answering in (
        "Hamro phone number: 061-417004. Aru kehi chahinchha bhane bhannus.",
        "Buff Jhol Momo Rs 250.",
        "Ho, hami Pokhara bhitra delivery garchau.",
        "Aru kehi sahayog chahiyo?",
    ):
        assert not asks_order_details(answering), answering


async def _endpoints() -> None:
    async with async_session_factory() as db:
        db.commit = db.flush  # stay inside the rolled-back transaction
        try:
            tenant = Tenant(name="Cube Cafe")
            owner = User(name="Owner", email="owner@close-conditions.test")
            db.add_all([tenant, owner])
            await db.flush()

            assert (await get_close_conditions(tenant)).closed is False

            out = await close_shop(CloseConditionsIn(message="Aaja kehi karanle banda chau, bholi 8 baje khulcha.", for_today=True),
                                   tenant, owner, db)
            assert out.closed and out.message == "Aaja kehi karanle banda chau, bholi 8 baje khulcha."
            assert out.until == hours.end_of_today()

            out = await close_shop(CloseConditionsIn(), tenant, owner, db)  # until reopened, default message
            assert out.closed and out.until is None and out.message == hours.DEFAULT_CLOSED_MESSAGE

            out = await close_shop(CloseConditionsIn(message="Banda", until=datetime.now(NEPAL).replace(tzinfo=None) + timedelta(hours=3)), tenant, owner, db)
            assert out.closed and out.until.tzinfo is not None  # a time without zone is Nepal time

            with pytest.raises(HTTPException) as e:
                await close_shop(CloseConditionsIn(until=datetime.now(NEPAL) - timedelta(minutes=1)), tenant, owner, db)
            assert e.value.status_code == 422

            out = await reopen_shop(tenant, owner, db)
            assert out.closed is False and out.message is None and out.custom_message == "Banda"  # kept for next time
            assert hours.close_conditions_message(tenant.close_conditions) is None
        finally:
            await db.rollback()


def test_close_conditions_endpoints():
    asyncio.run(_endpoints())
