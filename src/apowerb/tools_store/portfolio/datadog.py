"""Datadog logger tool — send application logs to Datadog Logs.

Submits one or more log entries to Datadog's HTTP Logs Intake API so an agent
can emit structured logs that show up in the user's Datadog account.

API contract
------------
Verified against https://docs.datadoghq.com/api/latest/logs/#send-logs and the
official Datadog Go client (DataDog/datadog-api-client-go, api_logs.go /
model_http_log_item.go), both consulted 2026-10-05:

* ``POST https://http-intake.logs.<site>/api/v2/logs``
* Headers: ``DD-API-KEY: <key>``, ``Content-Type: application/json``,
  ``Accept: application/json``.
* Body: a single log object, or an array of them. ``message`` is the only
  required field; ``ddsource``, ``ddtags``, ``hostname`` and ``service`` are
  optional, and extra attributes are accepted and indexed.
* Success: ``202 Accepted``. Errors come back as
  ``{"errors": [{"status", "title", "detail"}]}`` with 400/401/403/408/413/429
  /5xx (the invalid-key code is 401 or 403 — we treat both as auth failures).

Limits (from the intake docs / Go client): 5 MB uncompressed per payload, 1 MB
per log, 1000 entries per array. The tool enforces the array-count and a
conservative byte cap before sending so a runaway caller gets a clear error
instead of a 413.

The ``DD_API_KEY`` and ``DD_SITE`` module-level ``os.getenv`` calls are what the
ToolsStore parameter scanner reads to expose these as UI-configurable settings.
"""

from __future__ import annotations

import json
import os
from logging import getLogger

logger = getLogger(__name__)

# Read at module level so the ToolsStore parameter scanner (regex on os.getenv)
# discovers them for the UI. DD_SITE is the account's region; DD_API_KEY is the
# intake key (NOT an application key — intake only needs the API key).
_DD_API_KEY = os.getenv("DD_API_KEY", "")
_DD_SITE = os.getenv("DD_SITE", "us1")

# Datadog region → logs-intake host. The API key's account lives in exactly one
# site; sending to the wrong one is rejected. us1 is the default (no subdomain).
_SITE_HOSTS: dict[str, str] = {
    "us1": "http-intake.logs.datadoghq.com",
    "us3": "http-intake.logs.us3.datadoghq.com",
    "us5": "http-intake.logs.us5.datadoghq.com",
    "eu": "http-intake.logs.datadoghq.eu",
    "eu1": "http-intake.logs.datadoghq.eu",
    "ap1": "http-intake.logs.ap1.datadoghq.com",
    "ap2": "http-intake.logs.ap2.datadoghq.com",
    "us1-fed": "http-intake.logs.ddog-gov.com",
}

# Intake limits. Datadog rejects >1000 entries or >5 MB uncompressed; we cap a
# little under 5 MB to leave room for JSON framing and never trip a 413.
_MAX_ENTRIES = 1000
_MAX_PAYLOAD_BYTES = 5_000_000
_HTTP_TIMEOUT_S = 30


def _normalise_site(site: str) -> str:
    """Map a user-supplied site to its intake host, or raise ValueError.

    Accepts a region code ("us3"), the full intake host, or the account domain
    ("datadoghq.eu"). Matching is exact — a loose suffix match would silently
    route "com" to us1 and send logs to the wrong region.
    """
    key = (site or "").strip().lower()
    if key in _SITE_HOSTS:
        return _SITE_HOSTS[key]
    for host in _SITE_HOSTS.values():
        # Exact intake host, or the account domain it is built from
        # (host == "http-intake.logs." + domain).
        if key and (key == host or f"http-intake.logs.{key}" == host):
            return host
    raise ValueError(
        f"Unknown Datadog site '{site}'. Valid: {', '.join(sorted(_SITE_HOSTS))} "
        f"(use the region code shown in your Datadog account)."
    )


def _normalise_tags(ddtags: object) -> str | None:
    """Turn ``ddtags`` into Datadog's comma-separated ``key:value`` string.

    Accepts a list of strings or an already-formatted string. Returns None when
    no tags are given so the field is omitted entirely.
    """
    if ddtags is None:
        return None
    if isinstance(ddtags, str):
        return ddtags.strip() or None
    if isinstance(ddtags, (list, tuple)):
        parts = [str(t).strip() for t in ddtags if str(t).strip()]
        return ",".join(parts) or None
    raise ValueError("`ddtags` must be a list of strings or a comma-separated string.")


def _build_entry(
    message: str,
    *,
    service: str | None,
    ddsource: str,
    ddtags: object,
    hostname: str | None,
    status: str | None,
) -> dict:
    """Assemble one HTTPLogItem, omitting fields that were not provided."""
    entry: dict[str, str] = {"message": message, "ddsource": ddsource}
    tags = _normalise_tags(ddtags)
    if tags:
        entry["ddtags"] = tags
    if service:
        entry["service"] = service
    if hostname:
        entry["hostname"] = hostname
    if status:
        # Not a documented top-level field, but Datadog indexes extra attributes
        # and maps a "status" attribute to log level in the UI.
        entry["status"] = status
    return entry


def tool_send_datadog_log(
    message: str | None = None,
    service: str | None = None,
    ddsource: str = "apowerb",
    ddtags: object = None,
    hostname: str | None = None,
    status: str | None = None,
    site: str | None = None,
    messages: list[str] | None = None,
) -> dict:
    """Send one or more log entries to Datadog.

    Submits a log to the user's Datadog account via the HTTP Logs Intake API.
    Use it to emit a diagnostic line, an audit trail, or a batch of events that
    will be searchable in Datadog Logs.

    Args:
        message (str): The log line to send. Required unless ``messages`` is
            given. The only mandatory field of a Datadog log.
        service (str): The service name the log belongs to (e.g. "agent-runtime").
            Optional; groups logs in the Datadog UI.
        ddsource (str): The log source / technology (e.g. "python", "apowerb").
            Drives Datadog's parsing pipeline. Default: "apowerb".
        ddtags (list[str] | str): Tags as a list of "key:value" strings, or an
            already comma-separated string (e.g. "env:prod,team:bi"). Optional.
        hostname (str): The host the log originated from. Optional.
        status (str): Log level/status such as "info", "warn", "error". Optional;
            Datadog maps it to the level column.
        site (str): Datadog region code — one of us1, us3, us5, eu, ap1, ap2,
            us1-fed. Defaults to the DD_SITE setting (or us1). Must match the
            region of your DD_API_KEY.
        messages (list[str]): Send several log lines at once instead of one.
            Mutually complementary with ``message``; up to 1000 entries.

    Returns:
        dict: On success, ``status`` "success", ``entries_sent``, ``site``,
        ``http_status`` (202). On failure, ``status`` "error" with
        ``error_message`` (and ``http_status`` when the call reached Datadog).
    """
    import httpx

    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return {
            "status": "error",
            "error_message": "DD_API_KEY not set. Configure your Datadog API key.",
        }

    # Collect the lines to send from message and/or messages.
    lines: list[str] = []
    if message is not None:
        if not isinstance(message, str) or not message.strip():
            return {
                "status": "error",
                "error_message": "`message` must be a non-empty string.",
            }
        lines.append(message)
    if messages:
        if not isinstance(messages, list) or not all(
            isinstance(m, str) for m in messages
        ):
            return {
                "status": "error",
                "error_message": "`messages` must be a list of strings.",
            }
        lines.extend(m for m in messages if m.strip())

    if not lines:
        return {
            "status": "error",
            "error_message": "Provide `message` or a non-empty `messages` list.",
        }
    if len(lines) > _MAX_ENTRIES:
        return {
            "status": "error",
            "error_message": (
                f"Too many entries ({len(lines)}). Datadog accepts at most "
                f"{_MAX_ENTRIES} logs per request."
            ),
        }

    try:
        # Read DD_SITE at call time (like DD_API_KEY above) so a site configured
        # through the UI after import is honoured; _DD_SITE exists only so the
        # parameter scanner can surface the setting.
        host = _normalise_site(site or os.environ.get("DD_SITE") or "us1")
        entries = [
            _build_entry(
                line,
                service=service,
                ddsource=ddsource,
                ddtags=ddtags,
                hostname=hostname,
                status=status,
            )
            for line in lines
        ]
    except ValueError as exc:
        return {"status": "error", "error_message": str(exc)}

    # Send a single object when there is one entry, an array otherwise — both are
    # accepted, and the single-object form matches the documented minimal case.
    payload: object = entries[0] if len(entries) == 1 else entries
    body = json.dumps(payload).encode("utf-8")
    if len(body) > _MAX_PAYLOAD_BYTES:
        return {
            "status": "error",
            "error_message": (
                f"Payload too large ({len(body)} bytes). Datadog's limit is "
                f"{_MAX_PAYLOAD_BYTES} bytes uncompressed; send fewer/smaller logs."
            ),
        }

    url = f"https://{host}/api/v2/logs"
    try:
        logger.info("[DATADOG] Sending %d log(s) to %s", len(entries), host)
        resp = httpx.post(
            url,
            headers={
                "DD-API-KEY": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            content=body,
            timeout=_HTTP_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        logger.warning("[DATADOG] request failed: %s", exc)
        return {"status": "error", "error_message": f"Request to Datadog failed: {exc}"}

    if resp.status_code == 202:
        return {
            "status": "success",
            "entries_sent": len(entries),
            "site": host,
            "http_status": 202,
        }

    # Error: surface the HTTP status and Datadog's error detail if present.
    detail = resp.text[:500]
    if resp.status_code in (401, 403):
        detail = f"Authentication failed (check DD_API_KEY and site). {detail}"
    logger.warning("[DATADOG] HTTP %s: %s", resp.status_code, detail)
    return {
        "status": "error",
        "http_status": resp.status_code,
        "error_message": f"Datadog rejected the logs (HTTP {resp.status_code}). {detail}",
    }
