"""SQL generation building blocks for text-to-SQL and the Business Analyst layer.

These modules are intentionally PURE: no Google ADK, no live DB connection, no
agent state registry, no settings singleton. They operate on plain dicts and
strings so they can be unit-tested in isolation and reused by a higher-level
analysis/insight layer (the Business Analyst calls build_schema_prompt + a SQL
generator + execution + an interpretation pass via run_investigation).

Shape of ``schema_info`` (produced by text_to_sql._agent_fetch_schema):
    {
      "tables": {
        "<name>": {
          "columns": [{"column_name", "data_type", "is_nullable", ...}],
          "primary_keys": ["col", ...],
          "foreign_keys": [{"column_name", "foreign_table_name",
                            "foreign_column_name"}],
          "sample_data": [{col: value, ...}],
        }
      },
      "schema": "<schema name>",
      "db_type": "postgresql" | "mysql",
    }
"""

from apowerb.sqlgen.analyst import (
    AnalysisResult,
    InvestigationStep,
    run_investigation,
)
from apowerb.sqlgen.generator import extract_sql
from apowerb.sqlgen.safety import validate_sql_safety
from apowerb.sqlgen.schema_format import build_schema_prompt, compact_table_line
from apowerb.sqlgen.table_selection import score_and_order_tables

__all__ = [
    "extract_sql",
    "validate_sql_safety",
    "build_schema_prompt",
    "compact_table_line",
    "score_and_order_tables",
    "run_investigation",
    "AnalysisResult",
    "InvestigationStep",
]
