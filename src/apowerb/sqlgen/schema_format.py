"""Compact schema rendering for LLM prompts, within a hard char budget."""

from typing import Any, Dict, List, Optional

from apowerb.sqlgen.table_selection import score_and_order_tables

_MAX_SAMPLE_VAL = 40


def compact_table_line(name: str, info: Dict[str, Any]) -> str:
    """One line per table: ``name(col type PK, col type FK->other.col, ...)``."""
    pks = set(info.get("primary_keys") or [])
    fks = {
        fk["column_name"]: f"{fk['foreign_table_name']}.{fk['foreign_column_name']}"
        for fk in (info.get("foreign_keys") or [])
        if fk.get("column_name")
    }
    parts: List[str] = []
    for col in info.get("columns") or []:
        cn = col.get("column_name", "")
        seg = f"{cn} {col.get('data_type', '')}".rstrip()
        if cn in pks:
            seg += " PK"
        if cn in fks:
            seg += f" FK->{fks[cn]}"
        parts.append(seg)
    return f"{name}({', '.join(parts)})"


def _sample_lines(info: Dict[str, Any], sample_rows: int) -> List[str]:
    out: List[str] = []
    for row in (info.get("sample_data") or [])[:sample_rows]:
        cells = []
        for k, v in dict(row).items():
            sv = str(v)
            if len(sv) > _MAX_SAMPLE_VAL:
                sv = sv[:_MAX_SAMPLE_VAL] + "…"
            cells.append(f"{k}={sv}")
        if cells:
            out.append("  e.g. " + ", ".join(cells))
    return out


def _render_table(name: str, info: Dict[str, Any], include_samples: bool,
                  sample_rows: int) -> str:
    block = compact_table_line(name, info)
    if include_samples:
        sl = _sample_lines(info, sample_rows)
        if sl:
            block += "\n" + "\n".join(sl)
    return block


def build_schema_prompt(
    schema_info: Dict[str, Any],
    *,
    question: Optional[str] = None,
    max_chars: int = 16000,
    include_samples: bool = False,
    sample_rows: int = 1,
) -> str:
    """Render a compact schema description, capped at ``max_chars``.

    Tables are ordered by relevance to ``question`` then added greedily until
    the budget is hit; the rest are listed by name only so the model can ask
    for their columns via tool_get_database_schema. The single most relevant
    table is always included even if it alone exceeds the budget.
    """
    tables = schema_info.get("tables", {})
    db_schema = schema_info.get("schema", "public")
    db_type = schema_info.get("db_type", "postgresql")
    header = f"Database schema (schema: {db_schema}, type: {db_type}):"

    ordered = score_and_order_tables(schema_info, question)

    selected: List[str] = []
    omitted: List[str] = []
    used = len(header)
    for name in ordered:
        block = _render_table(name, tables[name], include_samples, sample_rows)
        cost = len(block) + 1  # newline
        if selected and used + cost > max_chars:
            omitted.append(name)
        else:
            selected.append(name)
            used += cost

    lines = [header]
    for name in selected:
        lines.append(_render_table(name, tables[name], include_samples, sample_rows))
    if omitted:
        lines.append("")
        lines.append(
            "Other tables (call tool_get_database_schema for their columns): "
            + ", ".join(sorted(omitted))
        )
    return "\n".join(lines)
