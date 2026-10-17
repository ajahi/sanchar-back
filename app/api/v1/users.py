"""Staff management — all operations scoped to the caller's tenant (spec §17)."""
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_email_verify_token, hash_password
from app.core.tenant import CurrentTenant, CurrentUser, require_role
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services.audit import record_audit
from app.services.email import send_staff_invite_email
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
        select(User)
        .where(User.tenant_id == tenant.id, User.status != "deleted")
        .order_by(User.created_at)
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
    bg: BackgroundTasks,
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
        verified=False,  # confirmed through the emailed invite link
        roles=roles,
    )
    db.add(new_user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        clash = (await db.execute(select(User.tenant_id).where(User.email == str(body.email)))).first()
        if clash is None:
            detail = f"The username '{body.username}' is already taken"
        elif clash[0] == tenant.id:
            detail = f"{body.email} is already on your team"
        else:
            detail = f"{body.email} is already registered to another account. Use a different email."
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

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
    bg.add_task(
        send_staff_invite_email,
        new_user.email,
        new_user.name,
        tenant.name,
        body.role,
        create_email_verify_token(str(new_user.id)),
    )
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

    # Only an owner may touch an owner account (an admin must not demote or lock out the owner).
    if "owner" in {r.name for r in target.roles} and "owner" not in {r.name for r in actor.roles}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only an owner can change an owner")

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


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[AdminRequired])
async def delete_user(
    user_id: uuid.UUID,
    tenant: CurrentTenant,
    actor: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft delete: the row stays (replies keep their sender name) but the login is dead and the email is freed."""
    target = (
        await db.execute(
            select(User).where(User.id == user_id, User.tenant_id == tenant.id, User.status != "deleted")
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == actor.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot remove yourself")
    if "owner" in {r.name for r in target.roles}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The owner cannot be removed")

    old_email = target.email
    target.status = "deleted"
    target.password_hash = None
    target.username = None
    target.email = f"deleted+{target.id}@removed.invalid"  # unique column: free the address for reuse
    await record_audit(
        db,
        tenant_id=tenant.id,
        actor_id=actor.id,
        action="user_deleted",
        entity_type="user",
        entity_id=target.id,
        meta={"email": old_email},
    )
    await db.commit()
