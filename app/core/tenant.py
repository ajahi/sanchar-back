"""Auth dependencies — the tenant-isolation backbone (spec §17)."""
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ACCESS_TOKEN, JWTError, decode_token
from app.db.session import get_db
from app.models.tenant import Tenant
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Decode the bearer access token and load the active User it identifies."""
    try:
        payload = decode_token(token)
    except JWTError:
        raise _CREDENTIALS_EXC
    if payload.get("type") != ACCESS_TOKEN:
        raise _CREDENTIALS_EXC

    user_id = payload.get("sub")
    if not user_id:
        raise _CREDENTIALS_EXC

    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        raise _CREDENTIALS_EXC
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_tenant(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Tenant:
    """Resolve the tenant the current user belongs to; every query filters on this."""
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise _CREDENTIALS_EXC
    return tenant


CurrentTenant = Annotated[Tenant, Depends(get_current_tenant)]


def require_role(*roles: str):
    """Dependency factory restricting a route to users holding any of the given roles."""

    async def _checker(user: CurrentUser) -> User:
        user_role_names = {r.name for r in user.roles}
        if not user_role_names & set(roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return _checker
