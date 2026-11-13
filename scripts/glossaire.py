#!/usr/bin/env python3
"""Glossaire métier généré depuis le code Python (ast) : la définition vit dans la docstring
ou dans Field(description=...), ce fichier ne fait que la recopier. Ne pas éditer la sortie.

Usage : python3 scripts/glossaire.py [--check] [--out docs/glossaire.md] [dossiers...]
--check : exit 1 si la sortie diffère du fichier existant (glossaire périmé).
"""

import ast
import sys
from pathlib import Path

args = sys.argv[1:]
check = "--check" in args
out = args[args.index("--out") + 1] if "--out" in args else "docs/glossaire.md"
skip = {args.index("--out") + 1} if "--out" in args else set()
roots = [a for i, a in enumerate(args) if not a.startswith("--") and i not in skip] or [
    "src"
]
root = Path.cwd()

ENUM_BASES = {"Enum", "StrEnum", "IntEnum"}
MODEL_BASES = {"BaseModel", "SQLModel", "Base", "DeclarativeBase", "TypedDict"}


def first_line(doc):
    return " ".join(doc.strip().split("\n\n")[0].split()) if doc else ""


def base_names(cls):
    return {
        b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in cls.bases
    }


def field_description(value):
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg in ("description", "doc", "comment") and isinstance(
                kw.value, ast.Constant
            ):
                return str(kw.value.value)
    return ""


sections = []
for r in roots:
    base = root / r
    for f in [base] if base.is_file() else sorted(base.rglob("*.py")):
        if any(
            p in f.parts
            for p in ("tests", ".venv", "node_modules", "__pycache__", "migrations")
        ):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        entries = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = base_names(node)
                doc = first_line(ast.get_docstring(node))
                if bases & ENUM_BASES:
                    vals = [
                        f"`{t.id}`"
                        for s in node.body
                        if isinstance(s, ast.Assign)
                        for t in s.targets
                        if isinstance(t, ast.Name)
                    ]
                    entries.append(
                        (
                            node.name,
                            f"{doc + ' ' if doc else ''}Valeurs : {', '.join(vals)}.",
                        )
                    )
                    continue
                if not doc and not bases & MODEL_BASES:
                    continue
                entries.append((node.name, doc or "(pas de docstring)"))
                for s in node.body:
                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                        d = field_description(s.value)
                        if d:
                            entries.append((f"{node.name}.{s.target.id}", d))
        if entries:
            rel = f.relative_to(root).as_posix()
            sections.append(
                f"## {rel}\n\n" + "\n".join(f"- **{n}** : {d}" for n, d in entries)
            )

roots_txt = ", ".join(f"`{r}`" for r in roots)
body = (
    "# Glossaire métier (généré)\n\n"
    f"> Généré par `python3 scripts/glossaire.py` depuis les docstrings de {roots_txt}.\n"
    "> Ne pas éditer ce fichier : corriger la docstring ou le `Field(description=...)` dans le code, puis régénérer.\n"
    "> « (pas de docstring) » marque un modèle sans définition : c'est une dette à combler.\n\n"
    + "\n\n".join(sections)
    + "\n"
)
target = root / out
if check:
    if not target.exists() or target.read_text(encoding="utf-8") != body:
        print(
            f"glossaire: {out} est périmé, lancer python3 scripts/glossaire.py",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"glossaire: {out} à jour")
else:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    print(f"glossaire: {out} écrit ({len(sections)} fichiers source)")
