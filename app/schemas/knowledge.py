"""Shop-context request/response schemas."""
from pydantic import BaseModel, Field


class SectionOut(BaseModel):
    key: str
    label: str
    hint: str
    content: str


class KnowledgeOut(BaseModel):
    sections: list[SectionOut]
    ai_enabled: bool
    max_chars: int


class SectionIn(BaseModel):
    content: str = Field(max_length=30_000)


class TestIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class TestOut(BaseModel):
    reply: str
    handover: bool
    reason: str
