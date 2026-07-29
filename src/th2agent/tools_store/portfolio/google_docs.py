"""Google Docs tools -- Read and write documents via Google Docs API.

Provides 3 tools for reading document content, creating new documents,
and appending text to existing documents.

Auth credentials are injected as environment variables by the tool_config
system at agent runtime:
  - ``GOOGLE_DOCS_REFRESH_TOKEN`` -- OAuth2 refresh token

The shared helper ``google_auth_headers()`` transparently exchanges the
refresh token for a short-lived access token.
"""

from logging import getLogger

import httpx

from th2agent.tools_store.portfolio.google_auth import google_auth_headers
from th2agent.tools_store.portfolio.integration_status import IntegrationStatusError

logger = getLogger(__name__)

_BASE = "https://docs.googleapis.com/v1/documents"
_SERVICE = "GOOGLE_DOCS"


def _extract_text(document: dict) -> str:
    """Walk document body and concatenate all text runs into plain text."""
    parts: list[str] = []
    for element in document.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts)


def tool_read_document(document_id: str) -> dict:
    """Read the full text content of a Google Doc.

    Args:
        document_id: The ID of the document (from the URL).

    Returns:
        A dict with status, title, content (plain text), and total_chars.
    """
    try:
        headers = google_auth_headers(_SERVICE)
        resp = httpx.get(
            f"{_BASE}/{document_id}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        doc = resp.json()
        content = _extract_text(doc)
        return {
            "status": "ok",
            "title": doc.get("title"),
            "content": content,
            "total_chars": len(content),
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_read_document failed")
        return {"status": "error", "message": str(exc), "retry": False}


def tool_create_document(title: str, content: str | None = None) -> dict:
    """Create a new Google Doc, optionally with initial text content.

    Args:
        title: Title of the new document.
        content: Optional text content to insert into the document body.

    Returns:
        A dict with status, document_id, title, and documentLink.
    """
    try:
        headers = google_auth_headers(_SERVICE)

        # Create the empty document
        resp = httpx.post(
            _BASE,
            headers=headers,
            json={"title": title},
            timeout=30,
        )
        resp.raise_for_status()
        doc = resp.json()
        doc_id = doc["documentId"]

        # Insert content if provided
        if content:
            resp = httpx.post(
                f"{_BASE}/{doc_id}:batchUpdate",
                headers=headers,
                json={
                    "requests": [
                        {
                            "insertText": {
                                "location": {"index": 1},
                                "text": content,
                            }
                        }
                    ]
                },
                timeout=30,
            )
            resp.raise_for_status()

        return {
            "status": "ok",
            "document_id": doc_id,
            "title": title,
            "documentLink": f"https://docs.google.com/document/d/{doc_id}/edit",
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_create_document failed")
        return {"status": "error", "message": str(exc), "retry": False}


def tool_append_text(document_id: str, text: str) -> dict:
    """Append text to the end of an existing Google Doc.

    Args:
        document_id: The ID of the document (from the URL).
        text: The text to append at the end of the document.

    Returns:
        A dict with status, document_id, and appended_chars.
    """
    try:
        headers = google_auth_headers(_SERVICE)

        # Get document to find the end index
        resp = httpx.get(
            f"{_BASE}/{document_id}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        doc = resp.json()
        end_index = doc["body"]["content"][-1]["endIndex"] - 1

        # Insert text at the end
        resp = httpx.post(
            f"{_BASE}/{document_id}:batchUpdate",
            headers=headers,
            json={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": end_index},
                            "text": text,
                        }
                    }
                ]
            },
            timeout=30,
        )
        resp.raise_for_status()

        return {
            "status": "ok",
            "document_id": document_id,
            "appended_chars": len(text),
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_append_text failed")
        return {"status": "error", "message": str(exc), "retry": False}
