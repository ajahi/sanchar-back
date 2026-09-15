"""Staff management — all operations scoped to the caller's tenant (spec §17)."""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services.audit import record_audit
from app.services.notifications import create_notification
from app.services.rbac import get_roles_by_names

router = APIRouter(prefix="/users", tags=["users"])

# Owner/admin only for the whole router.
AdminRequired = Depends(require_role("owner", "admin"))


@router.get("", response_model=list[UserOut], dependencies=[AdminRequired])
async def list_users(
    tenant: CurrentTenant,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[User]:
    result = await db.execute(
        select(User).where(User.tenant_id == tenant.id).order_by(User.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AdminRequired],
)
async def create_user(
    body: UserCreate,
    tenant: CurrentTenant,
    actor: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    roles = await get_roles_by_names(db, [body.role])
    if not roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown role: {body.role}"
        )

    new_user = User(
        tenant_id=tenant.id,
        name=body.name,
        email=str(body.email),
        phone_number=body.phone_number,
        username=body.username,
        password_hash=hash_password(body.password),
        roles=roles,
    )
    db.add(new_user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email or username already exists",
        )

    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=actor.id,
        action="user_created",
        entity_type="user",
        entity_id=new_user.id,
        meta={"roles": [r.name for r in new_user.roles]},
    )
    await create_notification(
        db,
        event="user_created",
        tenant_id=tenant.id,
        user_id=new_user.id,
        source_table="users",
        entity_id=new_user.id,
        subject="Welcome to the team",
        message=f"{new_user.name} was added as {body.role}.",
    )
    await db.commit()
    await db.refresh(new_user)
    return new_user


@router.patch("/{user_id}", response_model=UserOut, dependencies=[AdminRequired])
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    tenant: CurrentTenant,
    actor: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    # Tenant-scoped fetch: never load by id alone (spec §17).
    result = await db.execute(
        select(User).where(User.id == user_id, User.tenant_id == tenant.id)
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    changes = body.model_dump(exclude_unset=True)
    password = changes.pop("password", None)
    role_name = changes.pop("role", None)
    if password is not None:
        target.password_hash = hash_password(password)
    if role_name is not None:
        roles = await get_roles_by_names(db, [role_name])
        if not roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown role: {role_name}"
            )
        target.roles = roles
    for field, value in changes.items():
        setattr(target, field, value)

    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=actor.id,
        action="user_updated",
        entity_type="user",
        entity_id=target.id,
        meta={
            "fields": list(changes.keys())
            + (["password"] if password else [])
            + (["role"] if role_name else [])
        },
    )
    await db.commit()
    await db.refresh(target)
    return target
