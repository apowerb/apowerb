"""
bi/spec/templates_common.py
---------------------------
Building blocks shared by the spec v2 template modules: column-name checks, SQL quoting
and the 12-column grid layout helpers.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import IDENT, VisualSpec

_IDENT_RE = re.compile(IDENT)
Placed = list[tuple[VisualSpec, dict[str, int]]]


def check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'date_facture'."
        )
    return value


def dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def quote(column: str) -> str:
    return f'"{column}"'


def spread(count: int) -> list[int]:
    """Widths filling a 12-column row with ``count`` tiles."""
    base, extra = divmod(12, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def tile_row(items: list[VisualSpec], row: int, height: int) -> Placed:
    """One row of equal-width tiles."""
    placed: Placed = []
    col = 0
    for visual, width in zip(items, spread(len(items))):
        placed.append((visual, dict(row=row, col=col, width=width, height=height)))
        col += width
    return placed


def flow(items: list[VisualSpec], row: int, height: int) -> tuple[Placed, int]:
    """Two tiles per row; a last lone tile takes the full width."""
    placed: Placed = []
    for i in range(0, len(items), 2):
        pair = items[i : i + 2]
        widths = [12] if len(pair) == 1 else [6, 6]
        col = 0
        for visual, width in zip(pair, widths):
            placed.append((visual, dict(row=row, col=col, width=width, height=height)))
            col += width
        row += height
    return placed, row


def assemble(sources: list[BaseModel], filters: list[BaseModel], grid: Placed) -> dict:
    return {
        "sources": [dump(s) for s in sources],
        "filters": [dump(f) for f in filters],
        "components": [{"visual": dump(v), "position": pos} for v, pos in grid],
    }
