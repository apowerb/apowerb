"""Pydantic request/response schemas used by the RAG router endpoints."""

from typing import List, Optional

from pydantic import BaseModel


class IndexUrlRequest(BaseModel):
    agent_id: str
    url: str
    name: str
    session_id: Optional[str] = None


class IndexDbRequest(BaseModel):
    agent_id: str
    tool_config_id: str
    sql_query: str
    name: str
    session_id: Optional[str] = None


class IndexS3Request(BaseModel):
    agent_id: str
    tool_config_id: str
    s3_urls: List[str]
    name: str
    session_id: Optional[str] = None


class WebhookPayload(BaseModel):
    """Payload received from th2llm webhook notifications."""
    event: str
    knowledge_id: str
    status: str
    timestamp: Optional[str] = None
    processing: Optional[dict] = None


class DbCredentials(BaseModel):
    host: str
    port: int = 5432
    database: str
    user: str
    password: str
    schema_name: str = "public"


class IndexDbNlRequest(BaseModel):
    agent_id: str
    session_id: Optional[str] = None
    nl_description: str
    name: str
    credentials: Optional[DbCredentials] = None
    tool_config_id: Optional[str] = None
    save_connector: bool = False
    connector_name: Optional[str] = None
