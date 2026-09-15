"""User (staff/admin) request/response schemas."""
import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Tenant-facing roles only — a tenant admin can never grant platform roles
# (e.g. super_admin) through this API; those are assigned out-of-band.
RoleName = Literal["owner", "admin", "agent"]
Status = Literal["active", "inactive"]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: Optional[uuid.UUID] = None
    name: str
    # Plain str on output — emails are validated strictly on input (Create/Update);
    # re-validating stored data here would turn a legacy/edge value into a 500.
    email: str
    phone_number: Optional[str] = None
    username: Optional[str] = None
    verified: bool
    roles: list[str]
    status: str
    created_at: datetime
    updated_at: datetime

    @field_validator("roles", mode="before")
    @classmethod
    def _role_names(cls, v):
        """Accept the ORM's list[Role] relationship or an already-plain list[str]."""
        return [r.name if hasattr(r, "name") else r for r in v]


class UserCreate(BaseModel):
    name: str
    email: EmailStr
    phone_number: Optional[str] = None
    username: Optional[str] = None
    password: str = Field(min_length=8, max_length=128)
    role: RoleName = "agent"


class UserUpdate(BaseModel):
    name: Optional[str] = None
    phone_number: Optional[str] = None
    username: Optional[str] = None
    verified: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)
    role: Optional[RoleName] = None
    status: Optional[Status] = None
