"""Password hashing, JWT create/decode, and Fernet encryption for tokens at rest."""
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from cryptography.fernet import Fernet
from fastapi import Response
from jose import JWTError, jwt

from app.core.config import settings

# ---- Password hashing (bcrypt directly) ----
# bcrypt hashes at most the first 72 bytes; encode + truncate to stay within that.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    pw = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(pw, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---- JWT ----
ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"


def _create_token(
    subject: str, token_type: str, expires_delta: timedelta, **claims: Any
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        **claims,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(subject: str, *, tenant_id: Any, roles: list[str]) -> str:
    return _create_token(
        subject,
        ACCESS_TOKEN,
        timedelta(minutes=settings.access_token_expire_minutes),
        tenant_id=str(tenant_id) if tenant_id else None,
        roles=roles,
    )


def create_refresh_token(subject: str, *, tenant_id: Any) -> str:
    return _create_token(
        subject,
        REFRESH_TOKEN,
        timedelta(days=settings.refresh_token_expire_days),
        tenant_id=str(tenant_id) if tenant_id else None,
    )


OAUTH_STATE = "oauth_state"


def create_oauth_state(**claims: Any) -> str:
    """Signed, short-lived `state` for OAuth CSRF protection (carries e.g. source uri)."""
    return _create_token(
        "oauth",
        OAUTH_STATE,
        timedelta(minutes=settings.oauth_state_expire_minutes),
        **claims,
    )


def decode_oauth_state(token: str) -> dict[str, Any]:
    """Decode/validate an OAuth state token. Raises JWTError if invalid/expired/wrong type."""
    payload = decode_token(token)
    if payload.get("type") != OAUTH_STATE:
        raise JWTError("not an oauth state token")
    return payload


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises JWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def set_session_cookie(response: "Response", token: str) -> None:
    """Hand an access token to the dashboard as the httpOnly session cookie."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def clear_session_cookie(response: "Response") -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")


# ---- Token encryption at rest (Meta access tokens) ----
_fernet = Fernet(settings.token_encryption_key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()


__all__ = [
    "hash_password",
    "verify_password",
    "create_access_token",
    "create_refresh_token",
    "create_oauth_state",
    "decode_oauth_state",
    "decode_token",
    "set_session_cookie",
    "clear_session_cookie",
    "encrypt_token",
    "decrypt_token",
    "JWTError",
    "ACCESS_TOKEN",
    "REFRESH_TOKEN",
]
