"""SQL safety validation — SELECT-only, single-statement, no DDL/DML."""

import re
from typing import Optional, Tuple

_FORBIDDEN = [
    "DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE",
    "TRUNCATE", "GRANT", "REVOKE", "EXEC", "EXECUTE",
]


def validate_sql_safety(sql_query: str) -> Tuple[bool, Optional[str]]:
    """Return (is_safe, error). Allows a single SELECT statement only."""
    sql_upper = sql_query.upper().strip()
    if ";" in sql_query.strip().rstrip(";"):
        return False, "Multi-statement queries are not allowed"
    for kw in _FORBIDDEN:
        if re.search(rf"\b{kw}\b", sql_upper):
            return False, f"Query contains forbidden operation: {kw}"
    if not re.match(r"(SELECT|WITH)\b", sql_upper):
        return False, "Only SELECT (or WITH ... SELECT) queries are allowed"
    return True, None
