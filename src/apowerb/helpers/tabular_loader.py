"""Chargeur tabulaire partagé : octets + nom de fichier -> colonnes et lignes.

Utilisé par la prévision depuis une pièce jointe du chat
(``tools_store/portfolio/bi_datasets``) et par l'import du Data Pool BI
(``bi/data/upload_router``) : un seul lecteur, donc les mêmes formats et les
mêmes messages d'erreur des deux côtés.

Formats : CSV, TSV, TXT (séparateur détecté), XLSX, XLSM, XLS, ODS (moteur
calamine, déjà dépendance du cœur), JSON (liste d'objets, ou objet portant une
seule liste d'objets, ou JSON Lines) et Parquet (pyarrow, présent via
tabpfn-client).

Les erreurs sont des ``TabularLoadError`` au message français, destiné à être
renvoyé tel quel à l'utilisateur ou à l'agent. Un dépassement de ``max_rows``
est une erreur, jamais une troncature silencieuse.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from apowerb.schema.forecast_schema import MAX_DATA_ROWS

_DELIMITED = {".csv", ".tsv", ".txt"}
_SPREADSHEET = {".xlsx", ".xlsm", ".xls", ".ods"}
_JSON = {".json", ".jsonl"}
_PARQUET = {".parquet"}

SUPPORTED_EXTENSIONS = (
    ".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".ods",
    ".json", ".jsonl", ".parquet",
)

_CANDIDATE_DELIMITERS = (",", ";", "\t", "|")
_SNIFF_CHARS = 8192
_SNIFF_LINES = 20
_COMMA_DECIMAL = re.compile(r"-?\d+,\d+")
_INTEGER = re.compile(r"-?(0|[1-9]\d*)")
_FLOAT = re.compile(r"-?((0|[1-9]\d*)(\.\d+)?|\.\d+)([eE][+-]?\d+)?")
MAX_INPUT_BYTES = 50 * 1024 * 1024


class TabularLoadError(ValueError):
    """Fichier non lisible comme table ; le message est destiné à l'utilisateur."""


@dataclass(frozen=True)
class TabularData:
    columns: list[str]
    rows: list[dict[str, Any]]
    format: str
    sheet: str | None = None
    delimiter: str | None = None


def load_tabular(
    content: bytes,
    filename: str,
    *,
    sheet: str | None = None,
    max_rows: int = MAX_DATA_ROWS,
    max_bytes: int = MAX_INPUT_BYTES,
) -> TabularData:
    ext = os.path.splitext(os.path.basename(filename))[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise TabularLoadError(
            f"Format « {ext or filename} » non pris en charge. "
            f"Formats acceptés : {', '.join(SUPPORTED_EXTENSIONS)}."
        )
    if not content:
        raise TabularLoadError("Le fichier est vide.")
    if len(content) > max_bytes:
        raise TabularLoadError(
            f"Le fichier dépasse {max(max_bytes // (1024 * 1024), 1)} Mo, au-delà de la taille supportée."
        )

    sheet_name: str | None = None
    delimiter: str | None = None
    if ext in _DELIMITED:
        grid, delimiter = _read_delimited(content, ext)
    elif ext in _SPREADSHEET:
        grid, sheet_name = _read_spreadsheet(content, ext, sheet)
    elif ext in _JSON:
        grid = _read_json(content, ext)
    else:
        grid = _read_parquet(content)

    columns, rows = _grid_to_table(grid)
    if not rows:
        raise TabularLoadError("Le fichier ne contient aucune ligne de données.")
    if len(rows) > max_rows:
        raise TabularLoadError(
            f"Le fichier contient {len(rows)} lignes, au-delà du plafond de "
            f"{max_rows} lignes supporté."
        )
    return TabularData(
        columns=columns, rows=rows, format=ext.lstrip("."),
        sheet=sheet_name, delimiter=delimiter,
    )


def to_csv_bytes(data: TabularData) -> bytes:
    """CSV canonique (virgule, UTF-8, point décimal) relu par ``csv_executor``."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=data.columns, lineterminator="\n", extrasaction="ignore",
    )
    writer.writeheader()
    for row in data.rows:
        writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------- cellules


def _cell(value: Any) -> Any:
    """Valeur de cellule -> valeur JSON-sûre (dates en ISO, NaN -> None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value != value:  # NaT
            return None
        if value.tzinfo is None and value.time() == time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _cell(float(value))
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item") and not isinstance(value, (str, int, float, bool)):
        try:
            return _cell(value.item())
        except (ValueError, TypeError):
            return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _cast_text(text: str, decimal_comma: bool) -> Any:
    stripped = text.strip()
    if stripped == "":
        return None
    if _INTEGER.fullmatch(stripped):
        return int(stripped)
    candidate = stripped
    if decimal_comma and _COMMA_DECIMAL.fullmatch(stripped):
        candidate = stripped.replace(",", ".")
    if not _FLOAT.fullmatch(candidate):
        return text
    number = float(candidate)
    return number if math.isfinite(number) else text


# ---------------------------------------------------------------- lecteurs


def _decode(content: bytes) -> str:
    if b"\x00" in content[:_SNIFF_CHARS]:
        raise TabularLoadError(
            "Le contenu du fichier est binaire : impossible de le lire comme texte tabulaire."
        )
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _detect_delimiter(text: str) -> str:
    lines = [ln for ln in text[:_SNIFF_CHARS].splitlines() if ln.strip()][:_SNIFF_LINES]
    best, best_width = ",", 1
    for delim in _CANDIDATE_DELIMITERS:
        widths = {len(r) for r in csv.reader(lines, delimiter=delim)}
        if len(widths) == 1:
            width = next(iter(widths))
            if width > best_width:
                best, best_width = delim, width
    if best_width > 1:
        return best
    try:
        return csv.Sniffer().sniff(text[:_SNIFF_CHARS], delimiters="".join(_CANDIDATE_DELIMITERS)).delimiter
    except csv.Error:
        return ","


def _read_delimited(content: bytes, ext: str) -> tuple[list[list[Any]], str]:
    text = _decode(content)
    delim = "\t" if ext == ".tsv" else _detect_delimiter(text)
    decimal_comma = delim == ";"
    grid = [
        [_cast_text(c, decimal_comma) for c in record]
        for record in csv.reader(io.StringIO(text), delimiter=delim)
    ]
    return grid, delim


def _read_spreadsheet(content: bytes, ext: str, sheet: str | None) -> tuple[list[list[Any]], str]:
    import pandas as pd

    label = ext.lstrip(".")
    try:
        book = pd.ExcelFile(io.BytesIO(content), engine="calamine")
        names = [str(n) for n in book.sheet_names]
        if sheet is not None and sheet not in names:
            raise TabularLoadError(
                f"Feuille « {sheet} » introuvable. Feuilles disponibles : {', '.join(names)}."
            )
        target = sheet if sheet is not None else names[0]
        frame = book.parse(sheet_name=target, header=None, dtype=object)
    except TabularLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 -- calamine lève des types variés sur un fichier corrompu
        raise TabularLoadError(
            f"Fichier .{label} illisible ou corrompu ({type(exc).__name__})."
        ) from exc
    return [[_cell(c) for c in record] for record in frame.itertuples(index=False, name=None)], target


def _read_json(content: bytes, ext: str) -> list[list[Any]]:
    text = _decode(content)
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and ext == ".jsonl":
            payload = [payload]
    except json.JSONDecodeError:
        try:
            payload = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise TabularLoadError(f"JSON invalide : {exc.msg} (ligne {exc.lineno}).") from exc

    if isinstance(payload, dict):
        lists = [v for v in payload.values() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)]
        if len(lists) != 1:
            raise TabularLoadError(
                "JSON non tabulaire : attendu une liste d'objets, ou un objet contenant une seule liste d'objets."
            )
        payload = lists[0]
    if not isinstance(payload, list) or not payload or not all(isinstance(i, dict) for i in payload):
        raise TabularLoadError("JSON non tabulaire : attendu une liste d'objets (un objet par ligne).")

    columns: list[str] = []
    for record in payload:
        for key in record:
            if key not in columns:
                columns.append(key)
    return [list(columns)] + [[_cell(r.get(c)) for c in columns] for r in payload]


def _read_parquet(content: bytes) -> list[list[Any]]:
    import pandas as pd

    try:
        frame = pd.read_parquet(io.BytesIO(content))
    except ImportError as exc:
        raise TabularLoadError("Lecture Parquet indisponible (pyarrow absent).") from exc
    except Exception as exc:  # noqa: BLE001 -- pyarrow lève des types variés sur un fichier corrompu
        raise TabularLoadError(f"Fichier .parquet illisible ou corrompu ({type(exc).__name__}).") from exc
    return [[str(c) for c in frame.columns]] + [
        [_cell(c) for c in record] for record in frame.itertuples(index=False, name=None)
    ]


# ------------------------------------------------------------- grille -> table


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _grid_to_table(grid: list[list[Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    body = [r for r in grid if any(c is not None and c != "" for c in r)]
    if not body:
        raise TabularLoadError("Le fichier est vide.")

    width = max(len(r) for r in body)
    body = [r + [None] * (width - len(r)) for r in body]
    first = body[0]
    has_header = not all(_is_number(c) for c in first if c is not None and c != "")
    header_cells = first if has_header else [None] * width
    data = body[1:] if has_header else body

    keep = [
        i for i in range(width)
        if (header_cells[i] not in (None, "")) or any(r[i] not in (None, "") for r in data)
    ]
    columns: list[str] = []
    for i in keep:
        raw = header_cells[i]
        name = str(raw).strip() if raw not in (None, "") else ""
        base = name or f"colonne_{i + 1}"
        candidate, n = base, 2
        while candidate in columns:
            candidate = f"{base}_{n}"
            n += 1
        columns.append(candidate)

    rows = [
        {col: (None if r[i] == "" else r[i]) for col, i in zip(columns, keep)}
        for r in data
    ]
    return columns, rows
