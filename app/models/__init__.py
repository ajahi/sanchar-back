"""Model registry — importing this module registers every table on Base.metadata."""
from app.models.base import Base
from app.models.assignment import ConversationAssignment
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.handover import HandoverEvent
from app.models.knowledge import KnowledgeDocument
from app.models.message import Message
from app.models.notification import Notification
from app.models.permission import Permission, role_permissions
from app.models.role import Role, user_roles
from app.models.social_account import SocialAccount
from app.models.tenant import Tenant
from app.models.user import User

__all__ = [
    "Base",
    "Tenant",
    "User",
    "Role",
    "Permission",
    "user_roles",
    "role_permissions",
    "Customer",
    "SocialAccount",
    "Conversation",
    "Message",
    "ConversationAssignment",
    "HandoverEvent",
    "KnowledgeDocument",
    "AuditLog",
    "Notification",
]
