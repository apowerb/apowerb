"""Import d'un FEC (fichier des écritures comptables) comme jeu de données BI.

Les PME françaises exportent toutes un FEC (EBP, Sage, Cegid, Pennylane,
QuickBooks…) : texte séparé par tabulation ou barre verticale, ASCII,
ISO 8859-15 ou UTF-8 (avec ou sans BOM). ``parse_fec`` le normalise en un
DataFrame au contrat de 18 colonnes (noms minuscules, ordre légal) :

- ``debit``, ``credit`` : float, virgule décimale acceptée, vide → 0 ;
- ``montantdevise`` : float, vide → NULL ;
- ``ecrituredate``, ``piecedate``, ``datelet``, ``validdate`` : dates
  (AAAAMMJJ, AAAA-MM-JJ ou JJ/MM/AAAA), vide → NULL ;
- tout le reste : chaînes (zéros de tête conservés).

Les variantes à ``Montant`` + ``Sens`` (D/C) sont converties en débit/crédit.
Colonnes obligatoires : journalcode, ecriturenum, ecrituredate, comptenum,
comptelib, ecriturelib, et debit+credit (ou montant+sens). Les autres colonnes
légales absentes sont remplies de vide / NULL.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

import pandas as pd

from apowerb.helpers.tabular_loader import MAX_INPUT_BYTES, TabularData

FEC_COLUMNS: list[str] = [
    "journalcode", "journallib", "ecriturenum", "ecrituredate", "comptenum",
    "comptelib", "compauxnum", "compauxlib", "pieceref", "piecedate",
    "ecriturelib", "debit", "credit", "ecriturelet", "datelet", "validdate",
    "montantdevise", "idevise",
]
DATE_COLUMNS = ("ecrituredate", "piecedate", "datelet", "validdate")
_REQUIRED = (
    "journalcode", "ecriturenum", "ecrituredate", "comptenum", "comptelib",
    "ecriturelib",
)
_DETECT_MIN_MATCHES = 5
_SNIFF_BYTES = 8192
_DELIMITERS = ("\t", "|")
_DATE_FORMATS = (
    (re.compile(r"\d{8}"), "%Y%m%d"),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "%Y-%m-%d"),
    (re.compile(r"\d{2}/\d{2}/\d{4}"), "%d/%m/%Y"),
)
_SPACES = re.compile(r"[\s  ]")


class FecImportError(ValueError):
    """FEC illisible ; le message est destiné à l'utilisateur."""


def _decode(content: bytes) -> str:
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("iso-8859-15")


def _norm_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def _split_header(line: str) -> tuple[str, list[str]]:
    delim = max(_DELIMITERS, key=line.count)
    return delim, [_norm_name(c) for c in line.split(delim)]


def is_fec(content: bytes) -> bool:
    """Vrai si l'en-tête (tabulation ou barre verticale) est un en-tête FEC."""
    head = content[:_SNIFF_BYTES]
    if not head or b"\x00" in head:
        return False
    text = _decode_sample(head)
    first = next((ln for ln in text.split("\n") if ln.strip()), "")
    if not any(d in first for d in _DELIMITERS):
        return False
    _, names = _split_header(first.rstrip("\r"))
    return len(set(names) & set(FEC_COLUMNS)) >= _DETECT_MIN_MATCHES


def _decode_sample(head: bytes) -> str:
    """Décode un échantillon de début de fichier (peut finir au milieu d'un caractère UTF-8)."""
    try:
        return head.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data":
            return head[: exc.start].decode("utf-8-sig")
        return head.decode("iso-8859-15", errors="replace")


def _number(text: str, line: int, column: str) -> float:
    cleaned = _SPACES.sub("", text)
    if "," in cleaned and "." in cleaned:
        decimal = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        cleaned = cleaned.replace(thousands, "")
        cleaned = cleaned.replace(decimal, ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise FecImportError(f"Ligne {line} : montant invalide « {text} » dans la colonne {column}.")
    return value


def _parse_date(text: str, line: int, column: str) -> date:
    value = text.strip()
    for pattern, fmt in _DATE_FORMATS:
        if pattern.fullmatch(value):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                break
    raise FecImportError(
        f"Ligne {line} : date invalide « {text} » dans la colonne {column} "
        "(formats acceptés : AAAAMMJJ, AAAA-MM-JJ, JJ/MM/AAAA)."
    )


def parse_fec(content: bytes) -> pd.DataFrame:
    """Normalise un FEC en DataFrame au contrat (``FEC_COLUMNS``)."""
    if not content:
        raise FecImportError("Le fichier FEC est vide.")
    if len(content) > MAX_INPUT_BYTES:
        raise FecImportError(
            f"Le fichier dépasse {MAX_INPUT_BYTES // (1024 * 1024)} Mo, "
            "au-delà de la taille supportée."
        )
    if b"\x00" in content[:_SNIFF_BYTES]:
        raise FecImportError("Le contenu du fichier est binaire : ce n'est pas un FEC.")

    lines = _decode(content).split("\n")
    numbered = [(i, ln.rstrip("\r")) for i, ln in enumerate(lines, start=1) if ln.strip()]
    if not numbered:
        raise FecImportError("Le fichier FEC est vide.")
    _, header_line = numbered[0]
    delim, names = _split_header(header_line)
    while names and names[-1] == "":  # séparateur final
        names.pop()
    if delim not in header_line:
        raise FecImportError("En-tête FEC illisible : séparateur tabulation ou « | » attendu.")

    index = {n: i for i, n in enumerate(names) if n not in ("",)}
    missing = [c for c in _REQUIRED if c not in index]
    has_split = "debit" in index and "credit" in index
    has_signed = "montant" in index and "sens" in index
    if not has_split and not has_signed:
        missing += [c for c in ("debit", "credit") if c not in index]
    if missing:
        raise FecImportError(
            "Colonnes FEC manquantes : " + ", ".join(missing)
            + ". Colonnes reconnues : " + ", ".join(n for n in names if n) + "."
        )

    width = len(names)
    records: dict[str, list[Any]] = {c: [] for c in FEC_COLUMNS}
    for lineno, raw in numbered[1:]:
        fields = raw.split(delim)
        if len(fields) > width and not any(f.strip() for f in fields[width:]):
            fields = fields[:width]
        if len(fields) != width:
            raise FecImportError(
                f"Ligne {lineno} : {len(fields)} champs trouvés, {width} attendus "
                "(ligne mal formée)."
            )

        def cell(name: str) -> str:
            return fields[index[name]].strip() if name in index else ""

        if has_split:
            debit = _number(cell("debit"), lineno, "debit") if cell("debit") else 0.0
            credit = _number(cell("credit"), lineno, "credit") if cell("credit") else 0.0
        else:
            amount = _number(cell("montant"), lineno, "montant") if cell("montant") else 0.0
            sens = cell("sens").upper()
            if sens in ("D", "DEBIT", "DÉBIT"):
                debit, credit = amount, 0.0
            elif sens in ("C", "CREDIT", "CRÉDIT"):
                debit, credit = 0.0, amount
            else:
                raise FecImportError(
                    f"Ligne {lineno} : sens « {cell('sens')} » invalide (D ou C attendu)."
                )
        devise = cell("montantdevise")
        for col in FEC_COLUMNS:
            if col == "debit":
                records[col].append(debit)
            elif col == "credit":
                records[col].append(credit)
            elif col == "montantdevise":
                records[col].append(_number(devise, lineno, col) if devise else math.nan)
            elif col in DATE_COLUMNS:
                text = cell(col)
                records[col].append(_parse_date(text, lineno, col) if text else None)
            else:
                records[col].append(cell(col))

    if not records["journalcode"]:
        raise FecImportError("Le fichier FEC ne contient aucune écriture.")

    df = pd.DataFrame(records, columns=FEC_COLUMNS)
    for col in DATE_COLUMNS:
        df[col] = pd.to_datetime(df[col])
    return df


def fec_to_tabular(df: pd.DataFrame) -> TabularData:
    """DataFrame au contrat -> table à ranger en CSV canonique (dates ISO, NULL vide)."""
    rows: list[dict[str, Any]] = []
    for rec in df.to_dict("records"):
        row: dict[str, Any] = {}
        for col in FEC_COLUMNS:
            value = rec[col]
            if col in DATE_COLUMNS:
                row[col] = None if pd.isna(value) else value.strftime("%Y-%m-%d")
            elif col == "montantdevise":
                row[col] = None if pd.isna(value) else float(value)
            elif col in ("debit", "credit"):
                row[col] = float(value)
            else:
                row[col] = value
        rows.append(row)
    return TabularData(columns=list(FEC_COLUMNS), rows=rows, format="fec", delimiter=",")
