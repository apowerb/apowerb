"""PDF fictifs du jeu « mail AR / pas AR » (aucune donnée réelle).

    python tests/fixtures/workflow_mail_ar/make_pdfs.py <dossier>

écrit un PDF par mail de ``mails.json`` — utile pour essayer le workflow
d'exemple à la main (« Simuler un mail reçu »). Les tests appellent
``pdf_bytes`` directement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fpdf import FPDF

HERE = Path(__file__).parent


def mails() -> list[dict]:
    return json.loads((HERE / "mails.json").read_text(encoding="utf-8"))


def pdf_bytes(lines: list[str]) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    for line in lines:
        # Helvetica (latin-1) : suffisant pour ces textes fictifs.
        pdf.multi_cell(0, 8, line.encode("latin-1", "replace").decode("latin-1"), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    for mail in mails():
        (out / mail["file"]).write_bytes(pdf_bytes(mail["pdf"]))
        print(out / mail["file"])
