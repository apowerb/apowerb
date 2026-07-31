"""
core/db/write_config.py
-----------------------
Public core API: select the MSSQL write-capable DB config for an owner.

Extracted from ``tools_binder._select_mssql_write_config`` so that client
overlays (which rebind their own DB-writing tools at request time) can resolve
the right per-owner write config without importing core internals.

The two collaborators are injectable (keyword-only, default = lazy import of
the real ones) so the selection logic is unit-testable without a live DB.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def select_write_config(
    owner_id: str,
    *,
    _list_configs: Callable[[str], list] | None = None,
    _parse_ops: Callable[[object], set] | None = None,
) -> dict | None:
    """Return db_params of the first MSSQL write-capable config for *owner_id*.

    Selection criteria (all must match):
      - tool_category == "database"
      - owner_id != "system"  (system configs must never leak to real owners)
      - DB_TYPE in {mssql, sqlserver}
      - INSERT in DB_ALLOWED_OPS
    Ordered by numeric tool_config_id ascending; first match wins.
    Returns None when no candidate exists (caller falls back to os.environ).
    """
    if _list_configs is None:
        from apowerb.tools_store.tools_helpers import list_user_tool_configs as _list_configs
    if _parse_ops is None:
        from apowerb.tools_store.portfolio.database import _parse_allowed_ops as _parse_ops

    candidates = []
    for cfg in _list_configs(owner_id):
        if cfg.get("tool_category") != "database":
            continue
        if cfg.get("owner_id") == "system":
            continue
        params = cfg.get("tool_config_params") or {}
        db_type = (params.get("DB_TYPE") or "").lower()
        if db_type not in ("mssql", "sqlserver"):
            continue
        if "INSERT" not in _parse_ops(params.get("DB_ALLOWED_OPS")):
            continue
        raw_id = cfg.get("tool_config_id", "")
        try:
            numeric_id = int(str(raw_id).replace("tool_config", ""))
        except (ValueError, AttributeError):
            numeric_id = 999999
        candidates.append((numeric_id, params))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    chosen_id, chosen_params = candidates[0]
    logger.info(
        "[TO_AGENT] mssql_write_config selected: DB=%s (tool_config_id=%s) for owner=%s",
        chosen_params.get("DB_NAME"),
        chosen_id,
        owner_id,
    )
    return chosen_params
