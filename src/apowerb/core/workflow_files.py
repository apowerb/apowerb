"""Contenu des fichiers produits par le nœud ``file`` d'un workflow.

Fonctions pures : une valeur déjà rendue entre, des octets sortent. Le
stockage (référence opaque du propriétaire) est fait ailleurs
(``workflow_runtime.services_for``) ; ``workflow_graph`` applique les bornes
et lève les codes produit.

Formats tabulaires (csv, xlsx) : une liste d'objets (colonnes = clés, dans
l'ordre de première apparition) ou une liste de listes dont la première est
l'en-tête ; une chaîne JSON de l'un ou l'autre est acceptée, puisqu'un agent
rend souvent du texte. Formats texte (markdown, text, docx, pdf) : une chaîne,
lue comme du Markdown simple pour docx et pdf (titres, listes, tableaux,
paragraphes) — pas un moteur de mise en page.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Optional

# format -> (extension, type MIME)
FORMATS: dict[str, tuple[str, str]] = {
    "csv": (".csv", "text/csv"),
    "xlsx": (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "json": (".json", "application/json"),
    "markdown": (".md", "text/markdown"),
    "text": (".txt", "text/plain"),
    "docx": (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "pdf": (".pdf", "application/pdf"),
}
TABULAR = ("csv", "xlsx")
# Formats mis en page : leur rendu coûte au caractère (borné en amont).
RICH_TEXT = ("docx", "pdf")

# Une cellule qui commence ainsi est une formule pour Excel ou LibreOffice
# (injection CSV/XLSX) : elle est préfixée d'une apostrophe et reste du texte.
_FORMULA_START = ("=", "+", "@", "\t", "\r")
_NUMBER = re.compile(r"^-?\d+([.,]\d+)?$")
# Caractères de contrôle refusés par XML (xlsx, docx) : retirés.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class RowsError(ValueError):
    """La valeur ne se lit pas comme des lignes de tableau."""


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def table(value: Any) -> tuple[list[str], list[list[Any]]]:
    """(en-tête, lignes) d'une valeur tabulaire, ou ``RowsError``."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RowsError("texte qui n'est pas une liste JSON") from None
    if isinstance(value, dict):
        # Un objet seul : une ligne. Un objet qui enveloppe la liste
        # (``{"rows": [...]}``) est trop ambigu pour être deviné.
        value = [value]
    if not isinstance(value, list) or not value:
        raise RowsError("liste de lignes vide ou absente")
    if all(isinstance(r, dict) for r in value):
        header: list[str] = []
        for row in value:
            for key in row:
                if key not in header:
                    header.append(str(key))
        return header, [[row.get(k) for k in header] for row in value]
    if all(isinstance(r, list) for r in value):
        return [str(c) for c in value[0]], [list(r) for r in value[1:]]
    raise RowsError("lignes mêlant objets, listes ou valeurs simples")


def _cell(value: Any, neutralise: bool = True) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = _CONTROL.sub(
        "",
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str),
    )
    if neutralise and (
        text.startswith(_FORMULA_START)
        or (text.startswith("-") and not _NUMBER.match(text))
    ):
        return "'" + text
    return text


def _csv(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([_cell(h) for h in header])
    for row in rows:
        writer.writerow(["" if c is None else _cell(c) for c in row])
    # BOM : Excel ouvre alors les accents correctement.
    return buf.getvalue().encode("utf-8-sig")


def _xlsx(header: list[str], rows: list[list[Any]], sheet: Optional[str]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = sheet or "Data"
    # Pas d'apostrophe ici (elle resterait visible, ex. "+33…") : une cellule
    # de type texte n'est jamais évaluée comme formule.
    ws.append([_cell(h, neutralise=False) for h in header])
    for row in rows:
        ws.append([_cell(c, neutralise=False) for c in row])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in ws.iter_rows():
        for cell in r:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _docx(text: str) -> bytes:
    import docx

    doc = docx.Document()
    lines = text.splitlines()
    i = 0
    paragraph: list[str] = []

    def flush():
        if paragraph:
            doc.add_paragraph(" ".join(paragraph))
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
        elif m := _HEADING.match(line):
            flush()
            doc.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 4))
        elif m := _BULLET.match(line):
            flush()
            doc.add_paragraph(m.group(1).strip(), style="List Bullet")
        elif m := _NUMBERED.match(line):
            flush()
            doc.add_paragraph(m.group(1).strip(), style="List Number")
        elif (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEP.match(lines[i + 1])
        ):
            flush()
            header = _split_row(line)
            body = []
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                body.append(_split_row(lines[i]))
                i += 1
            tbl = doc.add_table(rows=1 + len(body), cols=len(header))
            tbl.style = "Table Grid"
            for row_cells, values in zip(tbl.rows, [header, *body]):
                for cell, value in zip(row_cells.cells, values):
                    cell.text = value
            continue
        else:
            paragraph.append(line.strip())
        i += 1
    flush()
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pdf_font(pdf) -> str:
    """DejaVu Sans livrée avec matplotlib (dépendance du projet) : l'image
    de production n'installe aucune police système, et Helvetica refuse
    « € », « — » ou « œ »."""
    import os

    import matplotlib

    folder = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    styles = {
        "": "DejaVuSans.ttf",
        "B": "DejaVuSans-Bold.ttf",
        "I": "DejaVuSans-Oblique.ttf",
        "BI": "DejaVuSans-BoldOblique.ttf",
    }
    paths = {k: os.path.join(folder, v) for k, v in styles.items()}
    if all(os.path.exists(p) for p in paths.values()):
        for style, path in paths.items():
            pdf.add_font("DejaVuSans", style, path)
        return "DejaVuSans"
    from apowerb.core.agent_helpers.pdf_writer import _setup_pdf_font

    return _setup_pdf_font(pdf)


def _pdf(text: str) -> bytes:
    from fpdf import FPDF

    from apowerb.core.agent_helpers.text_utils import _md_to_html

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_font(_pdf_font(pdf), size=11)
    pdf.write_html(_md_to_html(text))
    return bytes(pdf.output())


def build(
    fmt: str, value: Any, sheet: Optional[str] = None
) -> tuple[bytes, Optional[int]]:
    """(octets, nombre de lignes pour un format tabulaire) du fichier ``fmt``."""
    if fmt in TABULAR:
        header, rows = table(value)
        data = _csv(header, rows) if fmt == "csv" else _xlsx(header, rows, sheet)
        return data, len(rows)
    if fmt == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        return json.dumps(
            value, ensure_ascii=False, indent=2, default=str
        ).encode(), None
    text = _as_text(value)
    if fmt == "docx":
        return _docx(_CONTROL.sub("", text)), None
    if fmt == "pdf":
        return _pdf(_CONTROL.sub("", text)), None
    return text.encode("utf-8"), None


def text_length(value: Any) -> int:
    """Longueur du texte qu'un format mis en page aurait à rendre."""
    return len(_as_text(value))


def row_count(value: Any) -> int:
    """Nombre de lignes d'une valeur tabulaire (pour borner avant de construire)."""
    return len(table(value)[1])
