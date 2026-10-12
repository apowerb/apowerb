"""
bi/spec/sql_guard.py
--------------------
Static guard for every SQL fragment a spec carries (``where``, ``expr``,
``aggregates``, ``query``, ``MeasureSpec.expr``).

The engine runs them in an in-memory DuckDB with external access disabled
(``duckdb.py``); this guard is the first, readable line of defence: one
statement only, no DDL / DML / session commands, no file or environment
functions. Comments are removed, string literals and quoted identifiers are
never inspected for keywords.
"""

from __future__ import annotations

import re

from apowerb.bi.spec.errors import SpecError

MAX_FRAGMENT_LENGTH = 20_000

FORBIDDEN_WORDS = frozenset(
    {
        "ATTACH", "DETACH", "INSTALL", "LOAD", "COPY", "PRAGMA", "SET", "EXPORT",
        "IMPORT", "CALL", "CREATE", "DROP", "INSERT", "UPDATE", "DELETE", "ALTER",
        "TRUNCATE", "EXEC", "EXECUTE", "VACUUM", "CHECKPOINT", "PREPARE", "USE",
        "EXPLAIN", "DESCRIBE", "SUMMARIZE", "RECURSIVE", "READ_CSV", "READ_PARQUET", "READ_JSON",
        "GLOB",
    }
)
_WORD_RE = re.compile(r"\b(" + "|".join(sorted(FORBIDDEN_WORDS)) + r")\b", re.IGNORECASE)

# File, network, environment and catalog functions (name followed by "("), plus the
# functions that allocate an arbitrary amount of memory in one call (repeat, range…):
# DuckDB cannot interrupt them and an oversized request can kill the process.
_FORBIDDEN_FUNCTION_RE = re.compile(
    r"\b(repeat|lpad|rpad|space|range|generate_series|generate_subscripts|list_resize|array_resize|"
    r"repeat_row|list_repeat|array_repeat|read_[a-z0-9_]*|[a-z0-9_]*_scan|parquet_[a-z_]+|sniff_csv|glob|"
    r"duckdb_[a-z_]+|pragma_[a-z_]+|getenv|current_setting|query|query_table|"
    r"http[a-z_]*|load_extension|export_database|import_database|"
    r"[a-z_]*file[a-z_]*)\s*\(",
    re.IGNORECASE,
)
# FROM 'file.csv' / JOIN "file.parquet": a path used as a table.
_PATH_AS_TABLE_RE = re.compile(r"\b(from|join)\s*(?:'|\"[^\"]*[./\\])", re.IGNORECASE)
# Same after a comma of a FROM list: FROM t, '/etc/hosts'
_COMMA_PATH_RE = re.compile(r",\s*(?:'|\"[^\"]*[./\\])")
_FROM_RE = re.compile(r"\bfrom\b", re.IGNORECASE)
_FROM_END_RE = re.compile(
    r"\b(where|group|order|having|limit|union|except|intersect|qualify|window|offset|on|using)\b",
    re.IGNORECASE,
)
# Catalog / settings table functions, with or without parentheses.
_CATALOG_RE = re.compile(r"\b(duckdb_[a-z0-9_]*|pragma_[a-z0-9_]*)", re.IGNORECASE)


def _comma_path_in_from(cleaned: str, code: str) -> bool:
    """A string / path literal used as a table in the comma list of a FROM."""
    for m in _FROM_RE.finditer(code):
        depth, end = 0, len(code)
        for i in range(m.end(), len(code)):
            ch = code[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    end = i
                    break
        segment_code = code[m.end() : end]
        stop = _FROM_END_RE.search(segment_code)
        limit = m.end() + (stop.start() if stop else len(segment_code))
        # only the top level of the FROM list (parenthesised parts are sub-queries)
        flat, d = [], 0
        for i in range(m.end(), limit):
            c = code[i]
            if c == "(":
                d += 1
            elif c == ")":
                d -= 1
            flat.append(cleaned[i] if d == 0 else " ")
        if _COMMA_PATH_RE.search("".join(flat)):
            return True
    return False

_QUERY_START_RE = re.compile(r"^\s*(select|with|from)\b", re.IGNORECASE)


def _scan(sql: str) -> tuple[str, str]:
    """Return ``(cleaned, code)``.

    ``cleaned`` is ``sql`` without comments; ``code`` is ``cleaned`` with the
    content of string literals and quoted identifiers blanked, so keyword
    searches only see real SQL tokens.
    """
    cleaned: list[str] = []
    code: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end == -1 else end
            cleaned.append(" ")
            code.append(" ")
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                raise SpecError("Commentaire SQL non fermé.")
            i = end + 2
            cleaned.append(" ")
            code.append(" ")
            continue
        if ch in ("'", '"'):
            j = i + 1
            while True:
                if j >= n:
                    raise SpecError("Littéral ou identifiant SQL non fermé.")
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote = escape
                        j += 2
                        continue
                    break
                j += 1
            cleaned.append(sql[i : j + 1])
            code.append(ch + " " * (j - i - 1) + ch)
            i = j + 1
            continue
        cleaned.append(ch)
        code.append(ch)
        i += 1
    return "".join(cleaned), "".join(code)


def _check(sql: str, what: str, *, allow_trailing_semicolon: bool) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise SpecError(f"{what} : expression SQL vide.")
    if len(sql) > MAX_FRAGMENT_LENGTH:
        raise SpecError(f"{what} : expression SQL trop longue (max {MAX_FRAGMENT_LENGTH} caractères).")
    try:
        cleaned, code = _scan(sql)
    except SpecError as exc:
        raise SpecError(f"{what} : {exc}") from None
    if allow_trailing_semicolon:
        stripped = cleaned.rstrip()
        while stripped.endswith(";"):
            stripped = stripped[:-1].rstrip()
        trimmed = len(cleaned.rstrip()) - len(stripped)
        cleaned = stripped
        code = code[: len(code.rstrip()) - trimmed].rstrip()
    if ";" in code:
        raise SpecError(f"{what} : une seule instruction est permise (pas de « ; »).")
    depth = 0
    for ch in code:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        raise SpecError(f"{what} : parenthèses non équilibrées.")
    match = _WORD_RE.search(code)
    if match:
        raise SpecError(f"{what} : « {match.group(1).upper()} » n'est pas autorisé (lecture seule).")
    match = _FORBIDDEN_FUNCTION_RE.search(code)
    if match:
        raise SpecError(
            f"{what} : la fonction « {match.group(1)} » n'est pas autorisée "
            "(pas d'accès aux fichiers ni au système)."
        )
    match = _CATALOG_RE.search(code)
    if match:
        raise SpecError(f"{what} : « {match.group(1)} » n'est pas autorisé (catalogue système).")
    if _PATH_AS_TABLE_RE.search(cleaned) or _comma_path_in_from(cleaned, code):
        raise SpecError(f"{what} : lire un fichier n'est pas autorisé.")
    return cleaned.strip()


def check_expression(expr: str, what: str = "Expression") -> str:
    """Validate a scalar / boolean / aggregate fragment; return it without comments."""
    return _check(expr, what, allow_trailing_semicolon=False)


def check_query(query: str, what: str = "Requête") -> str:
    """Validate a full ``SELECT`` / ``WITH`` query; return it without comments.

    A trailing ``;`` is tolerated (it is dropped), anything after it is not.
    """
    cleaned = _check(query, what, allow_trailing_semicolon=True)
    if not _QUERY_START_RE.match(cleaned):
        raise SpecError(f"{what} : seules les requêtes SELECT sont permises.")
    return cleaned
