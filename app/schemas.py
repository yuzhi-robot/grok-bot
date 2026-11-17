"""Request/response schemas for the HTTP API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

MAX_MESSAGE_CHARS = 8000


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: Optional[str] = None
    persona: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class ConversationCreate(BaseModel):
    persona: Optional[str] = None
    title: Optional[str] = None


class ConversationRename(BaseModel):
    title: str = Field(..., min_length=1, max_length=40)


class PersonaInfo(BaseModel):
    id: str
    label: str
    description: str


class ConfigResponse(BaseModel):
    provider: str
    display_name: str
    model: str
    mode: str
    demo_mode: bool
    personas: List[PersonaInfo]
    default_persona: str
    hint: str
