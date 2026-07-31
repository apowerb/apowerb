"""Thaink2 API helpers: login, index document, create conversation and chat.

These functions are lightweight wrappers around HTTP calls and return
dicts with `status` and either the expected id or `error_message`.
"""

from typing import Dict, Any, Optional, List
import requests
import os
from pydantic import BaseModel, ValidationError, field_validator


class KnowledgePayload(BaseModel):
    name: str
    description: str
    prompt: str
    files: List[str]

    @field_validator("name")
    def name_must_not_be_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("name must be a non-empty string")
        return v

    @field_validator("description")
    def description_must_be_string(cls, v):
        if v is None:
            raise ValueError("description must be provided")
        return str(v)

    @field_validator("prompt")
    def prompt_must_be_string(cls, v):
        if v is None:
            raise ValueError("prompt must be provided")
        return str(v)

    @field_validator("files")
    def files_must_be_list_of_strings(cls, v):
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError("files must be a list of strings")
        if len(v) == 0:
            raise ValueError("files must contain at least one file identifier")
        return v


def _build_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path:
        return base
    return f"{base}{path if path.startswith('/') else '/' + path}"


def thaink2_login(
    base_url: str, username: str, password: str, auth_path: str = "/auth/login"
) -> Dict[str, Any]:
    """Authenticate and return a bearer token.

    Returns: {status: 'success', token: '...'} or {status: 'error', error_message: '...'}
    """
    url = _build_url(base_url, auth_path)
    headers = {"Content-Type": "application/json"}
    payload = {"username": username, "password": password}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Common token keys
        token = (
            data.get("access_token")
            or data.get("token")
            or (data.get("data") or {}).get("access_token")
        )

        if not token:
            return {
                "status": "error",
                "error_message": f"Token not found in response: {data}",
            }

        return {"status": "success", "token": token, "raw": data}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error_message": str(e)}


def index_document(
    base_url: str,
    token: str,
    document: Dict[str, Any],
    knowledge_path: str = "/knowledge",
) -> Dict[str, Any]:
    """Index a document under /knowledge endpoint.

    document: dict payload expected by the API or a KnowledgePayload instance.
    Returns: {status: 'success', knowledge_id: '...'} or error dict
    """
    # Use pydantic for validation
    try:
        if isinstance(document, KnowledgePayload):
            payload = document
        else:
            payload = KnowledgePayload(**document)
    except ValidationError as ve:
        return {"status": "error", "error_message": ve.errors()}
    except Exception as e:
        return {"status": "error", "error_message": str(e)}

    url = _build_url(base_url, knowledge_path)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        resp = requests.post(url, json=payload.dict(), headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        knowledge_id = (
            data.get("knowledge_id")
            or data.get("id")
            or (data.get("data") or {}).get("knowledge_id")
        )
        if not knowledge_id:
            return {
                "status": "error",
                "error_message": f"knowledge_id not found in response: {data}",
                "raw": data,
            }

        return {"status": "success", "knowledge_id": knowledge_id, "raw": data}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error_message": str(e)}


def create_conversation(
    base_url: str,
    token: str,
    knowledge_id: str,
    conversation_path: str = "/conversation",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a conversation linked to a knowledge resource.

    payload is optional extra fields for conversation creation.
    Returns: {status: 'success', conversation_id: '...'} or error dict
    """
    url = _build_url(base_url, conversation_path)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    body = {"knowledge_id": knowledge_id}
    if payload:
        body.update(payload)

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        conversation_id = (
            data.get("conversation_id")
            or data.get("id")
            or (data.get("data") or {}).get("conversation_id")
        )
        if not conversation_id:
            return {
                "status": "error",
                "error_message": f"conversation_id not found in response: {data}",
                "raw": data,
            }

        return {"status": "success", "conversation_id": conversation_id, "raw": data}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error_message": str(e)}


def create_chat(
    base_url: str,
    token: str,
    conversation_id: str,
    chat_path: str = "/conversation/{conversation_id}/messages",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a chat under a conversation via POST /conversation/{conversation_id}/messages.

    payload can include initial messages or settings.
    Returns: {status: 'success', chat_id: '...'} or error dict
    """
    # Interpolate conversation_id into the path
    path = chat_path.format(conversation_id=conversation_id)
    url = _build_url(base_url, path)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    body = {}
    if payload:
        body.update(payload)

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        chat_id = (
            data.get("chat_id")
            or data.get("id")
            or data.get("message_id")
            or (data.get("data") or {}).get("chat_id")
        )
        if not chat_id:
            return {
                "status": "error",
                "error_message": f"chat_id not found in response: {data}",
                "raw": data,
            }

        return {"status": "success", "chat_id": chat_id, "raw": data}

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error_message": str(e)}


def check_knowledge_complete(
    base_url: str,
    token: str,
    knowledge_id: str,
    knowledge_path: str = "/knowledge/{knowledge_id}",
) -> Dict[str, Any]:
    """Check if a knowledge base is complete.

    Queries GET /knowledge/{knowledge_id} to retrieve knowledge details and status.
    Returns: {status: 'success', is_complete: bool, raw: {...}} or error dict
    """
    # Interpolate knowledge_id into the path
    path = knowledge_path.format(knowledge_id=knowledge_id)
    url = _build_url(base_url, path)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # Extract knowledge data - may be nested under "data" key
        knowledge_data = data.get("data") or data

        # Check for common completion status fields
        is_complete = (
            knowledge_data.get("is_complete")
            or knowledge_data.get("completed")
            or knowledge_data.get("status") == "completed"
            or knowledge_data.get("status") == "complete"
        )

        return {
            "status": "success",
            "is_complete": bool(is_complete),
            "knowledge_id": knowledge_id,
            "raw": data,
        }

    except requests.exceptions.RequestException as e:
        return {"status": "error", "error_message": str(e)}


def tool_thaink2_rag(
    new_message: str,
    file_path: str = "",
    knowledge_id: str = "",
    conversation_id: str = "",
    base_url: str = "https://rag.thaink2.fr/",
    username: Optional[str] = None,
    password: Optional[str] = None,
    knowledge_name: str = "Knowledge Base",
    knowledge_description: str = "Indexed documents",
    knowledge_prompt: str = "You are a helpful assistant",
    max_wait_attempts: int = 30,
    wait_interval: int = 2,
) -> Dict[str, Any]:
    """
    Complete RAG workflow: index file (if provided), wait for completion,
    create conversation (if needed), and ask a question.

    Args:
            new_message: The question/message to ask the RAG
            file_path: Path to file to index (optional if knowledge_id already exists)
            knowledge_id: Existing knowledge base ID (skip indexation if provided)
            conversation_id: Existing conversation ID (skip conversation creation if provided)
            base_url: Base URL of Thaink2 RAG service
            username: Username for authentication (defaults to env var th2username)
            password: Password for authentication (defaults to env var th2password)
            knowledge_name: Name for the knowledge base
            knowledge_description: Description for the knowledge base
            knowledge_prompt: System prompt for the chatbot
            max_wait_attempts: Max attempts to check indexation completion
            wait_interval: Seconds between completion checks

    Returns:
            {status: 'success', message: '...', knowledge_id: '...', conversation_id: '...', response: {...}}
            or {status: 'error', error_message: '...'}
    """
    import time

    # Get credentials from env vars if not provided
    if not username:
        username = os.getenv("th2username")
    if not password:
        password = os.getenv("th2password")

    # Validate inputs
    if not new_message or not isinstance(new_message, str):
        return {
            "status": "error",
            "error_message": "new_message must be a non-empty string",
        }

    if not knowledge_id and not file_path:
        return {
            "status": "error",
            "error_message": "Either knowledge_id or file_path must be provided",
        }

    if not username or not password:
        return {
            "status": "error",
            "error_message": "username and password are required (set th2username and th2password env vars)",
        }

    # Step 1: Login
    login_result = thaink2_login(base_url, username, password)
    if login_result.get("status") != "success":
        return {
            "status": "error",
            "error_message": f"Login failed: {login_result.get('error_message')}",
        }

    token = login_result["token"]

    # Step 2: Index document if file_path provided
    if file_path and not knowledge_id:
        doc_payload = {
            "name": knowledge_name,
            "description": knowledge_description,
            "prompt": knowledge_prompt,
            "files": [file_path],
        }

        index_result = index_document(base_url, token, doc_payload)
        if index_result.get("status") != "success":
            return {
                "status": "error",
                "error_message": f"Indexation failed: {index_result.get('error_message')}",
            }

        knowledge_id = index_result["knowledge_id"]

    # Step 3: Wait for knowledge indexation to complete
    attempts = 0
    while attempts < max_wait_attempts:
        check_result = check_knowledge_complete(base_url, token, knowledge_id)
        if check_result.get("status") != "success":
            return {
                "status": "error",
                "error_message": f"Failed to check knowledge status: {check_result.get('error_message')}",
            }

        if check_result.get("is_complete"):
            break

        attempts += 1
        if attempts < max_wait_attempts:
            time.sleep(wait_interval)

    if not check_result.get("is_complete"):
        return {
            "status": "error",
            "error_message": f"Knowledge base not ready after {max_wait_attempts * wait_interval} seconds",
        }

    # Step 4: Create conversation if not provided
    if not conversation_id:
        conv_result = create_conversation(base_url, token, knowledge_id)
        if conv_result.get("status") != "success":
            return {
                "status": "error",
                "error_message": f"Conversation creation failed: {conv_result.get('error_message')}",
            }

        conversation_id = conv_result["conversation_id"]

    # Step 5: Ask the RAG
    chat_payload = {"message": new_message}
    chat_result = create_chat(base_url, token, conversation_id, payload=chat_payload)
    if chat_result.get("status") != "success":
        return {
            "status": "error",
            "error_message": f"Chat request failed: {chat_result.get('error_message')}",
        }

    return {
        "status": "success",
        "message": new_message,
        "knowledge_id": knowledge_id,
        "conversation_id": conversation_id,
        "response": chat_result.get("raw"),
    }
