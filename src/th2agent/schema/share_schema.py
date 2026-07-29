from pydantic import BaseModel
from typing import Any
from datetime import datetime

class ShareMessageIn(BaseModel):
    role: str
    content: str = ""
    timestamp: int | None = None
    toolCalls: list[Any] = []

class ShareCreateRequest(BaseModel):
    title: str = "Shared conversation"
    agentName: str = "Assistant"
    messages: list[ShareMessageIn]
    createdAt: int | None = None
    # When True, the share link can be read by anyone (anonymous visitors).
    # When False (default), only the owner can read or delete it.
    isPublic: bool = False

class ShareCreateResponse(BaseModel):
    shareId: str
    isPublic: bool = False

class SharedConversationResponse(BaseModel):
    title: str
    agentName: str
    messages: list[ShareMessageIn]
    createdAt: datetime | None