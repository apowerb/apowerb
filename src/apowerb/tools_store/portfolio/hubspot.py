"""HubSpot CRM tools — create and search contacts and deals.

Lets an agent write to and read the user's HubSpot CRM: create a contact,
create a deal, and search existing contacts.

API contract
------------
Verified against the HubSpot CRM v3 API reference (developers.hubspot.com),
consulted 2026-10-05:

* Base URL: ``https://api.hubapi.com``.
* Auth: a Private App access token in ``Authorization: Bearer <token>``.
* Create contact: ``POST /crm/v3/objects/contacts`` — body
  ``{"properties": {...}}``; returns 201 with the created object.
* Create deal: ``POST /crm/v3/objects/deals`` — body ``{"properties":
  {"dealname", "dealstage"?, "pipeline"?}}``; returns 201.
* Search contacts: ``POST /crm/v3/objects/contacts/search`` — body
  ``{"filterGroups": [...], "properties": [...], "limit": N}``; returns 200 with
  ``{"results": [...], "total": N, "paging": {...}}``.
* Errors: ``{"category", "message", "correlationId", ...}``.

``HUBSPOT_ACCESS_TOKEN`` is read at module level so the ToolsStore parameter
scanner surfaces it as a UI setting; it is also read at call time so a token
configured through the UI is honoured.
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Any

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# it for the UI. Re-read at call time in _token().
_HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

_BASE_URL = "https://api.hubapi.com"
_HTTP_TIMEOUT_S = 30
_SEARCH_MAX_LIMIT = 200  # HubSpot's documented per-page maximum.


def _token() -> str:
    """Return the HubSpot access token, read at call time, or raise."""
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN") or ""
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN not set")
    return token


def _request(method: str, path: str, *, json_body: dict | None = None) -> dict:
    """Call the HubSpot API and return the parsed JSON.

    Raises EnvironmentError (missing token) or propagates httpx errors.
    """
    import httpx

    token = _token()
    resp = httpx.request(
        method,
        f"{_BASE_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=_HTTP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _error_result(exc: Exception) -> dict:
    """Map an exception to the tool's error dict, keeping HTTP status if any."""
    import httpx

    if isinstance(exc, (EnvironmentError, ValueError)):
        return {"status": "error", "error_message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:500]
        if code in (401, 403):
            detail = f"Authentication failed (check HUBSPOT_ACCESS_TOKEN). {detail}"
        logger.warning("[HUBSPOT] HTTP %s: %s", code, detail)
        return {
            "status": "error",
            "http_status": code,
            "error_message": f"HubSpot error (HTTP {code}). {detail}",
        }
    logger.warning("[HUBSPOT] request failed: %s", exc)
    return {"status": "error", "error_message": f"Request to HubSpot failed: {exc}"}


def tool_hubspot_create_contact(
    email: str | None = None,
    firstname: str | None = None,
    lastname: str | None = None,
    phone: str | None = None,
    company: str | None = None,
    properties: dict | None = None,
) -> dict:
    """Create a contact in HubSpot.

    Args:
        email (str): The contact's email address. Strongly recommended — HubSpot
            uses it to deduplicate contacts. Required unless ``properties``
            already carries an identifying field.
        firstname (str): First name. Optional.
        lastname (str): Last name. Optional.
        phone (str): Phone number. Optional.
        company (str): Company name. Optional.
        properties (dict): Any additional HubSpot contact properties, merged with
            the named arguments above. Optional.

    Returns:
        dict: On success, ``status`` "success" with ``id`` and ``data`` (the
        created contact). On failure, ``status`` "error" with ``error_message``.
    """
    props: dict[str, Any] = {}
    if email:
        props["email"] = email
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if phone:
        props["phone"] = phone
    if company:
        props["company"] = company
    if properties:
        props.update(properties)

    if not props:
        return {
            "status": "error",
            "error_message": "Provide at least `email` (or another property).",
        }

    try:
        data = _request(
            "POST", "/crm/v3/objects/contacts", json_body={"properties": props}
        )
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    return {"status": "success", "id": data.get("id"), "data": data}


def tool_hubspot_create_deal(
    dealname: str | None = None,
    amount: float | None = None,
    dealstage: str | None = None,
    pipeline: str | None = None,
    close_date: str | None = None,
    properties: dict | None = None,
) -> dict:
    """Create a deal in HubSpot.

    Args:
        dealname (str): The deal name. Required.
        amount (float): Deal amount. Optional.
        dealstage (str): Internal ID of the deal stage. Optional (HubSpot uses
            the pipeline's first stage when omitted).
        pipeline (str): Internal ID of the pipeline. Optional (defaults to the
            account's default pipeline).
        close_date (str): Expected close date, ISO 8601 (e.g. "2026-12-31").
            Optional.
        properties (dict): Any additional HubSpot deal properties, merged with
            the named arguments above. Optional.

    Returns:
        dict: On success, ``status`` "success" with ``id`` and ``data`` (the
        created deal). On failure, ``status`` "error" with ``error_message``.
    """
    if not isinstance(dealname, str) or not dealname.strip():
        return {"status": "error", "error_message": "`dealname` is required."}

    props: dict[str, Any] = {"dealname": dealname.strip()}
    if amount is not None:
        props["amount"] = amount
    if dealstage:
        props["dealstage"] = dealstage
    if pipeline:
        props["pipeline"] = pipeline
    if close_date:
        props["closedate"] = close_date
    if properties:
        props.update(properties)

    try:
        data = _request(
            "POST", "/crm/v3/objects/deals", json_body={"properties": props}
        )
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    return {"status": "success", "id": data.get("id"), "data": data}


def tool_hubspot_search_contacts(
    query: str | None = None,
    email: str | None = None,
    limit: int = 10,
    properties: list[str] | None = None,
) -> dict:
    """Search contacts in HubSpot.

    Provide either a free-text ``query`` or an exact ``email`` to match.

    Args:
        query (str): Free-text search across default searchable properties.
            Optional if ``email`` is given.
        email (str): Match contacts whose email equals this value exactly.
            Optional if ``query`` is given.
        limit (int): Maximum results to return (1–200). Default: 10.
        properties (list[str]): Contact properties to return for each result.
            Optional.

    Returns:
        dict: On success, ``status`` "success" with ``count``, ``total`` and
        ``results``. On failure, ``status`` "error" with ``error_message``.
    """
    if not (query and query.strip()) and not (email and email.strip()):
        return {
            "status": "error",
            "error_message": "Provide `query` or `email` to search.",
        }

    body: dict[str, Any] = {"limit": max(1, min(limit, _SEARCH_MAX_LIMIT))}
    if email and email.strip():
        body["filterGroups"] = [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email.strip()}
                ]
            }
        ]
    if query and query.strip():
        body["query"] = query.strip()
    if properties:
        body["properties"] = properties

    try:
        data = _request("POST", "/crm/v3/objects/contacts/search", json_body=body)
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        return _error_result(exc)

    results = data.get("results") or []
    return {
        "status": "success",
        "count": len(results),
        "total": data.get("total"),
        "results": results,
    }
