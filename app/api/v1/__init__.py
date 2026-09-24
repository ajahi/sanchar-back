"""Aggregates all v1 routers under a single /api/v1 router."""
from fastapi import APIRouter

from app.api.v1 import (
    auth,
    conversations,
    dashboard,
    notifications,
    social_accounts,
    tenants,
    users,
    webhooks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(tenants.router)
api_router.include_router(users.router)
api_router.include_router(social_accounts.router)
api_router.include_router(notifications.router)
api_router.include_router(conversations.router)
api_router.include_router(dashboard.router)
api_router.include_router(webhooks.router)
