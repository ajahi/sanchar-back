"""Dashboard summary returned by GET /api/v1/dashboard."""
from pydantic import BaseModel


class TopQuery(BaseModel):
    text: str
    count: int


class DashboardOut(BaseModel):
    window_days: int
    # Every channel key is always present (0 when idle), so the UI layout never shifts.
    new_messages: dict[str, int]
    top_queries: list[TopQuery]
    messages_handled: dict[str, int]
    handover: dict[str, int]
