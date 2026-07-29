from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List


class MessagePart(BaseModel):
    text: str


class NewMessage(BaseModel):
    role: str
    parts: List[MessagePart]


class RunADKAgentRequest(BaseModel):
    """Schema for running an ADK agent."""

    agent_name: str
    user_id: str
    session_id: str
    data: Optional[Dict[str, Any]] = Field(default_factory=dict)
    run_mode: str = "run"
    streaming: bool = False
    new_message: Dict[str, Any]


class UpdateADKAgentSessionRequest(BaseModel):
    """Schema for updating an ADK agent session."""

    agent_name: str
    user_id: str
    session_id: str
    data: Optional[Dict[str, Any]] = Field(default_factory=dict)


class CreateADKAgentSessionRequest(BaseModel):
    """Schema for creating an ADK agent session."""

    agent_name: str
    user_id: str
    session_id: str
    data: Optional[Dict[str, Any]] = Field(default_factory=dict)


class DeleteADKAgentSessionRequest(BaseModel):
    """Schema for deleting an ADK agent session."""

    agent_name: str
    user_id: str
    session_id: str


class GenerateTitleRequest(BaseModel):
    """Schema for auto-generating a conversation title from its first message."""

    message: str
    agent_id: Optional[str] = None
