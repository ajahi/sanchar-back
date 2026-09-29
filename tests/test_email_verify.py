"""Register -> unverified login blocked -> verify link -> login works. Postgres, cleans up after.

    python -m pytest tests/test_email_verify.py
"""
import asyncio

from fastapi import BackgroundTasks, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, select
from starlette.responses import Response

from app.api.v1 import auth
from app.core.security import create_email_verify_token
from app.db.session import async_session_factory
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import RegisterRequest, VerifyRequest

EMAIL = "verify-test@example.com"


async def _run() -> None:
    sent: list[tuple] = []

    async def fake_send(to, name, token):
        sent.append((to, token))

    auth.send_verification_email = fake_send
    try:
        async with async_session_factory() as db:
            bg = BackgroundTasks()
            await auth.register(
                RegisterRequest(name="T", business_name="Shop", email=EMAIL, password="password123"), db, bg
            )
            await bg()
            assert len(sent) == 1

            form = OAuth2PasswordRequestForm(username=EMAIL, password="password123")
            try:
                await auth.login(form, db, Response())
                raise AssertionError("unverified login should fail")
            except HTTPException as e:
                assert e.status_code == 403

            # a bad / wrong-type token is rejected
            try:
                await auth.verify_email(VerifyRequest(token="junk"), db, Response())
                raise AssertionError("junk token accepted")
            except HTTPException as e:
                assert e.status_code == 400

            user = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one()
            resp = Response()
            await auth.verify_email(VerifyRequest(token=create_email_verify_token(str(user.id))), db, resp)
            assert "ns_session" in resp.headers["set-cookie"]
            await auth.login(form, db, Response())  # now allowed
    finally:
        async with async_session_factory() as db:
            u = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one_or_none()
            if u:
                tid = u.tenant_id
                await db.delete(u)
                await db.flush()
                await db.execute(delete(Tenant).where(Tenant.id == tid))
                await db.commit()


def test_email_verification_flow():
    asyncio.run(_run())
