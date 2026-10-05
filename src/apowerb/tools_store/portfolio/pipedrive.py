"""Pipedrive CRM tools — create and search deals and people.

Lets an agent write to and read the user's Pipedrive CRM: create a person
(contact), create a deal, and search existing deals.

API contract
------------
Verified against https://pipedrive.readme.io/docs/core-api-concepts-authentication
and the Deals/Persons API reference (developers.pipedrive.com), consulted
2026-10-05:

* Base URL is per-account: ``https://<company-domain>.pipedrive.com/api/v2``.
* Auth: the API token goes in the ``x-api-token`` header (not a query param).
* Create person: ``POST /persons`` — ``name`` required; ``emails`` is a list of
  ``{value, primary, label}`` objects.
* Create deal: ``POST /deals`` — ``title`` required.
* Search deals: ``GET /deals/search?term=<>`` — ``term`` is required (min 2
  chars, or 1 with ``exact_match``).
* Responses use the envelope ``{"success": bool, "data": {...}}``; a failure is
  ``{"success": false, "error": "...", ...}``. 2xx is success.

``PIPEDRIVE_API_TOKEN`` and ``PIPEDRIVE_COMPANY_DOMAIN`` are read at module level
so the ToolsStore parameter scanner surfaces them as UI settings; both are also
read at call time so a value configured through the UI is honoured.
"""

from __future__ import annotations

import os
import re
from logging import getLogger
from typing import Any

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# them for the UI. Both are re-read at call time in _config().
_PIPEDRIVE_API_TOKEN = os.getenv("PIPEDRIVE_API_TOKEN", "")
_PIPEDRIVE_COMPANY_DOMAIN = os.getenv("PIPEDRIVE_COMPANY_DOMAIN", "")

_HTTP_TIMEOUT_S = 30
# A company domain is the subdomain before .pipedrive.com — letters, digits and
# hyphens only. We validate it so a malformed value cannot bend the request to
# another host.
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _config() -> tuple[str, str]:
    """Return (api_token, company_domain), read at call time.

    Raises EnvironmentError when either is missing, or ValueError when the
    company domain is not a bare subdomain label.
    """
    token = os.environ.get("PIPEDRIVE_API_TOKEN") or ""
    domain = (os.environ.get("PIPEDRIVE_COMPANY_DOMAIN") or "").strip()
    # Tolerate a full URL / host being pasted in: keep only the subdomain label.
    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.split(".", 1)[0].strip("/")
    if not token:
        raise EnvironmentError("PIPEDRIVE_API_TOKEN not set")
    if not domain:
        raise EnvironmentError(
            "PIPEDRIVE_COMPANY_DOMAIN not set (your Pipedrive subdomain, e.g. "
            "'acme' for acme.pipedrive.com)"
        )
    if not _DOMAIN_RE.match(domain):
        raise ValueError(
            f"Invalid PIPEDRIVE_COMPANY_DOMAIN '{domain}': expected a bare "
            f"subdomain label (letters, digits, hyphens)."
        )
    return token, domain


def _request(
    method: str, path: str, *, params: dict | None = None, json_body: dict | None = None
) -> dict:
    """Call the Pipedrive v2 API and return the parsed envelope.

    Raises EnvironmentError/ValueError (bad config) or propagates httpx errors.
    The returned dict is Pipedrive's ``{"success", "data", ...}`` envelope.
    """
    import httpx

    token, domain = _config()
    url = f"https://{domain}.pipedrive.com/api/v2{path}"
    resp = httpx.request(
        method,
        url,
        headers={"x-api-token": token, "Content-Type": "application/json"},
        params=params,
        json=json_body,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    envelope = resp.json()
    # Pipedrive can answer 2xx with success=false in the envelope (e.g. a
    # validation refusal); raise_for_status does not catch that, so a bare read
    # would report success with no data. Surface it as an error instead.
    if isinstance(envelope, dict) and envelope.get("success") is False:
        raise RuntimeError(envelope.get("error") or "Pipedrive returned success=false")
    return envelope


def _error_result(exc: Exception) -> dict:
    """Map an exception to the tool's error dict, keeping HTTP status if any."""
    import httpx

    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"status": "error", "error_message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:500]
        if code in (401, 403):
            detail = f"Authentication failed (check PIPEDRIVE_API_TOKEN). {detail}"
        logger.warning("[PIPEDRIVE] HTTP %s: %s", code, detail)
        return {
            "status": "error",
            "http_status": code,
            "error_message": f"Pipedrive error (HTTP {code}). {detail}",
        }
    logger.warning("[PIPEDRIVE] request failed: %s", exc)
    return {"status": "error", "error_message": f"Request to Pipedrive failed: {exc}"}


def tool_pipedrive_create_person(
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    org_id: int | None = None,
    owner_id: int | None = None,
) -> dict:
    """Create a person (contact) in Pipedrive.

    Args:
        name (str): The person's full name. Required.
        email (str): Primary email address. Optional.
        phone (str): Primary phone number. Optional.
        org_id (int): ID of the organization to link the person to. Optional.
        owner_id (int): ID of the Pipedrive user who owns the person. Optional.

    Returns:
        dict: On success, ``status`` "success" with ``id`` and ``data`` (the
        created person). On failure, ``status`` "error" with ``error_message``.
    """
    if not isinstance(name, str) or not name.strip():
        return {"status": "error", "error_message": "`name` is required."}

    body: dict[str, Any] = {"name": name.strip()}
    if email:
        body["emails"] = [{"value": email, "primary": True, "label": "work"}]
    if phone:
        body["phones"] = [{"value": phone, "primary": True, "label": "work"}]
    if org_id is not None:
        body["org_id"] = org_id
    if owner_id is not None:
        body["owner_id"] = owner_id

    try:
        envelope = _request("POST", "/persons", json_body=body)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    data = envelope.get("data") or {}
    return {"status": "success", "id": data.get("id"), "data": data}


def tool_pipedrive_create_deal(
    title: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    person_id: int | None = None,
    org_id: int | None = None,
    stage_id: int | None = None,
    owner_id: int | None = None,
) -> dict:
    """Create a deal in Pipedrive.

    Args:
        title (str): The deal title. Required.
        value (float): Monetary value of the deal. Optional.
        currency (str): Currency code (e.g. "USD", "EUR"). Optional.
        person_id (int): ID of the linked person. Optional.
        org_id (int): ID of the linked organization. Optional.
        stage_id (int): ID of the pipeline stage to place the deal in. Optional.
        owner_id (int): ID of the Pipedrive user who owns the deal. Optional.

    Returns:
        dict: On success, ``status`` "success" with ``id`` and ``data`` (the
        created deal). On failure, ``status`` "error" with ``error_message``.
    """
    if not isinstance(title, str) or not title.strip():
        return {"status": "error", "error_message": "`title` is required."}

    body: dict[str, Any] = {"title": title.strip()}
    if value is not None:
        body["value"] = value
    if currency:
        body["currency"] = currency
    if person_id is not None:
        body["person_id"] = person_id
    if org_id is not None:
        body["org_id"] = org_id
    if stage_id is not None:
        body["stage_id"] = stage_id
    if owner_id is not None:
        body["owner_id"] = owner_id

    try:
        envelope = _request("POST", "/deals", json_body=body)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    data = envelope.get("data") or {}
    return {"status": "success", "id": data.get("id"), "data": data}


def tool_pipedrive_search_deals(
    term: str | None = None,
    status: str | None = None,
    person_id: int | None = None,
    limit: int = 50,
) -> dict:
    """Search deals in Pipedrive by a text term.

    Args:
        term (str): Text to search for in deal titles/fields. Required, at least
            2 characters.
        status (str): Filter by deal status — "open", "won" or "lost". Optional.
        person_id (int): Only deals linked to this person. Optional.
        limit (int): Maximum number of results to return. Default: 50.

    Returns:
        dict: On success, ``status`` "success" with ``count`` and ``items`` (the
        matched deals). On failure, ``status`` "error" with ``error_message``.
    """
    if not isinstance(term, str) or len(term.strip()) < 2:
        return {
            "status": "error",
            "error_message": "`term` is required and must be at least 2 characters.",
        }

    params: dict[str, Any] = {"term": term.strip(), "limit": limit}
    if status:
        params["status"] = status
    if person_id is not None:
        params["person_id"] = person_id

    try:
        envelope = _request("GET", "/deals/search", params=params)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    data = envelope.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    items = items or []
    return {"status": "success", "count": len(items), "items": items}
