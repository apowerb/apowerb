"""SQL safety validation — SELECT-only, single-statement, no DDL/DML.

Read-only also rules out SELECT forms that write (INTO), lock rows, or call
functions that sleep, touch the server's files or other connections. Checks
run on the query with comments and string literals set aside.
"""

import re
from typing import Optional, Tuple

_FORBIDDEN = [
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    # SELECT ... INTO creates a table (Postgres, SQL Server) or writes a file
    # (MySQL INTO OUTFILE / DUMPFILE): a write that starts with SELECT.
    "INTO",
    # SQL Server's way to stall a connection.
    "WAITFOR",
]

# A read-only analytics query never needs these, and each one turns a SELECT
# into something else: stalling the connection, reading the server's files,
# reaching another database, killing sessions or changing session settings.
_FORBIDDEN_FUNCTIONS = [
    r"PG_SLEEP\w*",
    "SLEEP",
    "BENCHMARK",
    r"PG_READ_\w+",
    "PG_LS_DIR",
    "PG_STAT_FILE",
    r"DBLINK\w*",
    "PG_TERMINATE_BACKEND",
    "PG_CANCEL_BACKEND",
    "SET_CONFIG",
    r"LO_\w+",
    "PG_RELOAD_CONF",
    "PG_ROTATE_LOGFILE",
    "PG_LOGDIR_LS",
    r"PG_FILE_\w+",
    r"PG_ADVISORY_\w+",
    r"PG_TRY_ADVISORY_\w+",
    "NEXTVAL",
    "SETVAL",
]

_UNMODELLED_QUOTING = re.compile(r"\$\w*\$|`|/\*!")

_LINE_END = re.compile(r"[\r\n]")
_QUOTING_INSIDE_BRACKETS = re.compile(r"['\"]|--|/\*")

# Row locks: SELECT ... FOR UPDATE is already caught by UPDATE above.
_LOCKING_CLAUSE = re.compile(r"\bFOR\s+(?:KEY\s+)?SHARE\b")


def _views(sql: str) -> tuple[str, str] | None:
    """Two upper-cased readings of ``sql``, or None if it cannot be read.

    Comments become a space in both. String literals become ``''`` in both, so
    a keyword quoted as text is not mistaken for SQL. Quoted identifiers become
    ``""`` in the keyword view (a column named "into" is a name, not a clause)
    but lose only their quotes in the function view, so ``"pg_sleep"(1)`` is
    still seen as a call. Returns None when the query cannot be read safely: an
    unterminated literal or comment, a literal holding a backslash, or quoting
    the reader does not model.
    """
    # Quoting this reader does not model would let it lose track of where a
    # literal ends: Postgres dollar quotes ($$...$$, $tag$...$tag$), MySQL
    # backticks, and MySQL /*! ... */ comments, whose content MySQL executes.
    if _UNMODELLED_QUOTING.search(sql):
        return None
    keywords: list[str] = []
    functions: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "#":
            # A comment to end of line in MySQL, an operator in Postgres: the
            # two databases would not read the rest of the line the same way.
            # Cost: Postgres jsonb operators (#>, #>>, #-) are refused too.
            return None
        if ch == "-" and sql.startswith("--", i):
            # Ends at a newline or a bare carriage return, as in Postgres.
            end = _LINE_END.search(sql, i)
            i = n if end is None else end.end()
            keywords.append(" ")
            functions.append(" ")
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                return None
            i = end + 2
            keywords.append(" ")
            functions.append(" ")
        elif ch == "[":
            # SQL Server reads [...] as an identifier, Postgres as a subscript.
            # Either way it is read as SQL here; only a span holding quoting
            # or a comment marker could hide something, so that is refused.
            j = sql.find("]", i)
            # "]]" is an escaped "]" inside a SQL Server identifier.
            if (
                j == -1
                or sql.startswith("]]", j)
                or _QUOTING_INSIDE_BRACKETS.search(sql, i, j)
            ):
                return None
            keywords.append(ch)
            functions.append(ch)
            i += 1
        elif ch == "'":
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j == -1:
                    return None
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            # A backslash makes the end of the literal ambiguous (E'\'' in
            # Postgres, 'it\'s' in MySQL): what closes it here may not close it
            # in the database, and a call could hide behind the difference.
            if "\\" in sql[i:j]:
                return None
            i = j + 1
            keywords.append("''")
            functions.append("''")
        elif ch == '"':
            j = sql.find('"', i + 1)
            if j == -1:
                return None
            keywords.append('""')
            functions.append(sql[i + 1 : j])
            i = j + 1
        else:
            keywords.append(ch)
            functions.append(ch)
            i += 1
    return "".join(keywords).upper().strip(), "".join(functions).upper()


def validate_sql_safety(sql_query: str) -> Tuple[bool, Optional[str]]:
    """Return (is_safe, error). Allows a single SELECT statement only."""
    views = _views(sql_query)
    if views is None:
        return (
            False,
            "Query uses quoting or comments that cannot be checked safely",
        )
    sql_upper, sql_calls = views
    if ";" in sql_upper.rstrip(";"):
        return False, "Multi-statement queries are not allowed"
    for kw in _FORBIDDEN:
        if re.search(rf"\b{kw}\b", sql_upper):
            return False, f"Query contains forbidden operation: {kw}"
    for fn in _FORBIDDEN_FUNCTIONS:
        match = re.search(rf"\b{fn}\s*\(", sql_calls)
        if match:
            name = match.group(0).rstrip("( ")
            return False, f"Query calls a forbidden function: {name}"
    if _LOCKING_CLAUSE.search(sql_upper):
        return False, "Row-locking clauses (FOR SHARE, FOR KEY SHARE) are not allowed"
    if not re.match(r"(SELECT|WITH)\b", sql_upper):
        return False, "Only SELECT (or WITH ... SELECT) queries are allowed"
    return True, None
