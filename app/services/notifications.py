"""Helper for creating notification records.

This records the notification (and, for in_app, it's immediately usable). Actually
dispatching email/sms/push is a later concern — a worker would pick up `status='pending'`
rows for non-in_app media and flip them to sent/failed.
"""
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification


async def create_notification(
    db: AsyncSession,
    *,
    event: str,
    tenant_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    source_table: Optional[str] = None,
    entity_id: Optional[uuid.UUID] = None,
    medium: str = "in_app",
    subject: Optional[str] = None,
    message: Optional[str] = None,
    payload: Optional[dict] = None,
) -> Notification:
    """Add (not commit) a notification row; the caller commits in its own transaction."""
    note = Notification(
        tenant_id=tenant_id,
        user_id=user_id,
        source_table=source_table,
        entity_id=entity_id,
        event=event,
        medium=medium,
        # in_app notifications are "delivered" the moment they're stored.
        status="sent" if medium == "in_app" else "pending",
        subject=subject,
        message=message,
        payload=payload or {},
    )
    db.add(note)
    return note
