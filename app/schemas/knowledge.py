"""Shop-context request/response schemas."""
import uuid

from pydantic import BaseModel, Field


class TopicOut(BaseModel):
    id: uuid.UUID
    title: str  # the owner's own topic name, e.g. "Courses", "Nail art prices"
    content: str  # the answers the bot gives on this topic
    hint: str = ""  # example text, kept from the starter suggestion it was added from


class Suggestion(BaseModel):
    title: str
    hint: str


class KnowledgeOut(BaseModel):
    topics: list[TopicOut]
    suggestions: dict[str, list[Suggestion]]  # business type -> starter topics (one tap to add)
    ai_enabled: bool
    max_chars: int


class TopicIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(default="", max_length=30_000)
    hint: str = Field(default="", max_length=500)


class TestIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class TestOut(BaseModel):
    reply: str
