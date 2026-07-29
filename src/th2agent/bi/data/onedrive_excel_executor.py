"""
bi/data/onedrive_excel_executor.py
----------------------------------
OneDrive Excel/CSV query executor for BI charts.

Reads a file stored on the user's OneDrive via Microsoft Graph and returns
its rows as ``list[dict]`` — suitable for the chart data pipeline.

Unlike the Google Drive executor, OneDrive credentials are **not** stored in
a per-chart tool connection config. Instead we look up the user's
``Integration`` row (``provider='microsoft_onedrive'``) and use its
refresh_token under a short-lived ``env_scope`` to obtain a Graph bearer
token — exactly the pattern used by ``routers/onedrive_browser.py``.

Source options accepted
-----------------------
``source.source_options`` supports the following keys:

- ``item_path`` (str, required — unless ``item_id`` is given): path of the
  file relative to the drive root, e.g. ``"Reports/campaign.xlsx"``.
- ``item_id``  (str, optional fallback): Graph driveItem id. Used only when
  ``item_path`` is not provided — resolved to the path via a Graph call.
- ``sheet_name`` (str | int, optional): which worksheet to read in .xlsx
  files. Ignored for .csv. Defaults to the first sheet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from th2agent.bi.charts.core import DataSource
from th2agent.helpers.encryptor import decrypt_value
from th2agent.helpers.env_scope import env_scope
from th2agent.models import Integration, User
from th2agent.tools_store.portfolio.integration_status import IntegrationStatusError
from th2agent.tools_store.portfolio.onedrive_core import (
    _GRAPH_BASE,
    _graph_headers,
    download_and_parse_spreadsheet,
)

logger = logging.getLogger(__name__)

# Serialize access to ONEDRIVE_REFRESH_TOKEN so two concurrent BI fetches
# from different tenants never observe each other's value while the token
# exchange is running — same pattern as the browser router.
_onedrive_env_lock = asyncio.Lock()


class OnedriveExcelQueryExecutor:
    """Executes a BI query against a OneDrive-hosted Excel or CSV file.

    The executor never raises on business errors. Any failure is returned as
    a single-row ``[{"error": "..."}]`` list, consistent with the contract
    enforced by :class:`GoogleDriveQueryExecutor`.
    """

    def __init__(
        self,
        *,
        owner_id: str | None,
        db_session: AsyncSession | None,
    ) -> None:
        self._owner_id = owner_id
        self._db = db_session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, source: DataSource) -> list[dict[str, Any]]:
        if not self._owner_id:
            logger.error("OneDrive Excel executor requires an owner_id for tenant isolation")
            return [{"error": "Missing owner_id for OneDrive Excel source"}]

        if self._db is None:
            logger.error("OneDrive Excel executor requires a db_session to resolve credentials")
            return [{"error": "Missing db_session for OneDrive Excel source"}]

        opts = source.source_options or {}
        item_path = opts.get("item_path")
        item_id = opts.get("item_id")
        sheet_name = opts.get("sheet_name")

        if not item_path and not item_id:
            logger.error("OneDrive Excel source requires 'item_path' (or 'item_id') in source_options")
            return [{"error": "Missing 'item_path' in source_options for OneDrive Excel source"}]

        try:
            refresh_token = await self._resolve_refresh_token()
        except IntegrationStatusError as exc:
            logger.error(
                "OneDrive Excel executor: structured auth error %s — %s",
                exc.code, exc.message,
            )
            return [{
                "error": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            }]
        except RuntimeError as exc:
            logger.error("OneDrive Excel executor could not resolve refresh token: %s", exc)
            return [{"error": str(exc)}]

        # Acquire a Graph bearer header under env_scope, then release the
        # scope — headers can be reused safely after exit.
        try:
            async with env_scope(
                {"ONEDRIVE_REFRESH_TOKEN": refresh_token},
                lock=_onedrive_env_lock,
            ):
                headers = _graph_headers()
        except IntegrationStatusError as exc:
            logger.error(
                "OneDrive Excel executor token exchange returned %s: %s",
                exc.code, exc.message,
            )
            return [{
                "error": exc.message,
                "code": exc.code,
                "provider": exc.provider,
                "remediable_by_reconnect": exc.is_remediable_by_reconnect,
            }]
        except RuntimeError as exc:
            logger.error("OneDrive Excel executor token exchange failed: %s", exc)
            return [{"error": str(exc)}]
        except Exception as exc:  # pragma: no cover — defensive
            logger.error("Unexpected error while acquiring OneDrive headers: %s", exc)
            return [{"error": f"OneDrive authentication error: {exc}"}]

        resolved_path = item_path
        if not resolved_path:
            try:
                resolved_path = self._resolve_path_from_id(item_id, headers)
            except Exception as exc:
                logger.error("Failed to resolve OneDrive item_id %s to a path: %s", item_id, exc)
                return [{"error": f"Could not resolve OneDrive item_id '{item_id}' to a path: {exc}"}]

        try:
            df, err = await asyncio.to_thread(
                download_and_parse_spreadsheet,
                resolved_path,
                headers,
                sheet_name=sheet_name,
            )
        except Exception as exc:
            logger.error("OneDrive Excel download/parse crashed for %s: %s", resolved_path, exc)
            return [{"error": f"Failed to read OneDrive file: {exc}"}]

        if err is not None or df is None:
            logger.debug("OneDrive Excel executor received parse error: %s", err)
            return [{"error": err or "Unknown OneDrive read error"}]

        # Replace pandas NaN / NaT with None before serialising. Plain
        # ``df.to_dict()`` leaves ``float('nan')`` in the output, which is
        # invalid JSON and crashes downstream consumers (LLM providers like
        # Vertex AI reject ``NaN`` tokens in their payload). ``to_jsonable``
        # then coerces any remaining numpy scalars (``numpy.bool_``, int64…)
        # — pydantic/ADK event serialisation rejects them and tears down the
        # SSE stream mid-response.
        import pandas as pd  # local import to keep the module import-light
        from th2agent.helpers.jsonify import to_jsonable
        records = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")
        records = [to_jsonable(r) for r in records]
        limit = source.limit or len(records)
        return records[:limit]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_refresh_token(self) -> str:
        """Return the decrypted OneDrive refresh_token for ``self._owner_id``.

        Raises ``RuntimeError`` if no Integration row is found or the token
        is missing — same contract as
        :func:`routers.onedrive_browser._resolve_onedrive_refresh_token`.
        """
        assert self._db is not None  # guarded by run()
        # owner_id can reach us as either the int user_id (public endpoint) or
        # the user's email (authenticated endpoint passes _user.email). Handle
        # both: int → direct lookup; string that looks like an email → join
        # via User.email to recover the user_id.
        stmt = select(Integration).where(Integration.provider == "microsoft_onedrive")
        raw_owner = self._owner_id
        try:
            stmt = stmt.where(Integration.user_id == int(raw_owner))
        except (TypeError, ValueError):
            stmt = stmt.join(User, User.user_id == Integration.user_id).where(
                User.email == raw_owner
            )
        result = await self._db.execute(stmt)
        integration = result.scalar_one_or_none()

        if not integration or not integration.refresh_token:
            raise RuntimeError(
                "OneDrive credentials are not configured. "
                "The user must connect their OneDrive account via the integrations settings page."
            )

        try:
            return decrypt_value(integration.refresh_token)
        except InvalidToken:
            logger.warning(
                "OneDrive refresh_token for user_id=%s is plaintext — "
                "run `python -m th2agent.cli.migrate_integrations --encrypt-legacy`.",
                self._owner_id,
            )
            return integration.refresh_token

    def _resolve_path_from_id(self, item_id: str, headers: dict[str, str]) -> str:
        """Resolve a Graph driveItem id to its path relative to the drive
        root. Only used when the caller passes ``item_id`` instead of
        ``item_path``.
        """
        resp = httpx.get(
            f"{_GRAPH_BASE}/me/drive/items/{item_id}",
            headers=headers,
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Graph metadata request for item_id={item_id} returned HTTP {resp.status_code}"
            )
        meta = resp.json()
        name = meta.get("name")
        parent_ref = meta.get("parentReference") or {}
        parent_path = parent_ref.get("path") or ""
        # parent_path looks like "/drive/root:/Reports" — strip the prefix.
        prefix = "/drive/root:"
        if parent_path.startswith(prefix):
            parent_path = parent_path[len(prefix):]
        # Build a clean POSIX-style relative path
        rel = f"{parent_path.strip('/')}/{name}".strip("/")
        return rel
