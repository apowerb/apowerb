"""
bi/data/agent_executor.py
-------------------------
Query executor that fetches data by running ADK agents.

Each agent is invoked via ``run_adk_agent()`` (HTTP POST to the ADK server).
The text response is parsed as JSON rows and concatenated across all requested
agents.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import timedelta
from typing import Any

import aiohttp

from th2agent.bi.charts.core import DataSource
from th2agent.configs.settings import get_settings
from th2agent.core.agent_main import get_agent_by_id, get_agent_folder_name
from th2agent.helpers.security import create_access_token

settings = get_settings()

logger = logging.getLogger(__name__)


class AgentQueryExecutor:
    """Runs one or more ADK agents and collects their output as data rows."""

    def __init__(self, user_id: str, token: str | None = None) -> None:
        self._user_id = user_id
        self._token = token or self._internal_token(user_id)

    @staticmethod
    def _internal_token(user_email: str) -> str:
        return create_access_token(
            data={"sub": user_email, "type": "access"},
            expires_delta=timedelta(minutes=30),
        )

    async def run(self, source: DataSource) -> list[dict[str, Any]]:
        agent_ids: list[int | str] = source.source_options.get("agent_ids", [])
        if not agent_ids:
            logger.warning("[AGENT_EXECUTOR] No agent_ids in source_options — returning empty")
            return []

        all_rows: list[dict[str, Any]] = []

        for aid in agent_ids:
            agent_info = get_agent_by_id(str(aid), self._user_id)
            if not agent_info:
                logger.warning("[AGENT_EXECUTOR] Agent %s not found for user %s — skipping", aid, self._user_id)
                continue

            agent_name = get_agent_folder_name(agent_info["agent_name"])
            session_id = f"bi-agent-{uuid.uuid4().hex[:12]}"
            message_text = source.source_options.get(
                "message",
                source.query or "Return the latest data as a JSON array of objects.",
            )
            new_message = {"role": "user", "parts": [{"text": message_text}]}

            try:
                url = f"{settings.root_path}/api/adk/run"
                payload = {
                    "agent_name": agent_name,
                    "user_id": self._user_id,
                    "session_id": session_id,
                    "new_message": new_message,
                    "run_mode": "run",
                    "streaming": False,
                }
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                }
                # Bounded total timeout: a hung agent must fail fast, not
                # freeze the dashboard for 15 minutes (was total=None).
                _total = int(os.getenv("BI_AGENT_TIMEOUT_S", "120"))
                timeout = aiohttp.ClientTimeout(
                    total=_total, connect=15, sock_read=_total
                )
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.post(url, json=payload, headers=headers) as resp:
                        resp.raise_for_status()
                        result = await resp.json()
            except Exception:
                logger.exception("[AGENT_EXECUTOR] run_adk_agent failed for agent %s", aid)
                continue

            logger.info("[AGENT_EXECUTOR] Raw result type=%s, preview=%s", type(result).__name__, str(result)[:500])
            rows = self._parse_response(result)
            all_rows.extend(rows)

        return all_rows

    @staticmethod
    def _parse_response(result: Any) -> list[dict[str, Any]]:
        """Extract JSON rows from the agent response text."""
        # /api/adk/run may return a list of event dicts or a single dict
        if isinstance(result, list):
            # Extract text from the last event that has content
            text = ""
            for event in reversed(result):
                if isinstance(event, dict):
                    for key in ("response", "text", "content"):
                        val = event.get(key, "")
                        if val:
                            text = val
                            break
                    # ADK events may nest content in parts
                    parts = event.get("content", {}).get("parts", []) if isinstance(event.get("content"), dict) else []
                    for p in parts:
                        if isinstance(p, dict) and p.get("text"):
                            text = p["text"]
                            break
                if text:
                    break
        elif isinstance(result, dict):
            text = result.get("response", "") or result.get("text", "") or ""
        else:
            text = str(result) if result else ""
        if not text:
            return []

        # Try parsing the full text as JSON
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
            if isinstance(data, dict):
                return [data]
        except (json.JSONDecodeError, TypeError):
            pass

        # Try extracting JSON from markdown code blocks
        for marker in ("```json", "```"):
            if marker in text:
                start = text.index(marker) + len(marker)
                end = text.find("```", start)
                if end != -1:
                    snippet = text[start:end].strip()
                    try:
                        data = json.loads(snippet)
                        if isinstance(data, list):
                            return [r for r in data if isinstance(r, dict)]
                        if isinstance(data, dict):
                            return [data]
                    except (json.JSONDecodeError, TypeError):
                        pass

        # Text was present but no JSON rows could be extracted. Returning []
        # here would render an empty chart with no explanation (the prior
        # silent-failure symptom). Surface it so the service maps it to 502.
        preview = text[:300]
        logger.warning(
            "[AGENT_EXECUTOR] Could not parse agent response as JSON rows; preview=%s",
            preview,
        )
        raise ValueError(f"Agent returned non-JSON output (preview: {preview!r})")
