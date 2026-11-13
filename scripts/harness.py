#!/usr/bin/env python3
"""Harnais de règles strictes : regex par fichier, bibliothèque standard seulement.

Usage : python3 scripts/harness.py [chemins...] [--update-baseline] [--accept-frozen]
Même format de harness.config.json que harness.mjs. Règle "strict" : zéro occurrence.
Règle "ratchet" : jamais plus que la baseline du fichier. Règle "frozen" : fichiers figés.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

args = sys.argv[1:]
flags = {a for a in args if a.startswith("--")}
paths = [a for a in args if not a.startswith("--")]
root = Path.cwd()
config = json.loads((root / "harness.config.json").read_text(encoding="utf-8"))
baseline_path = root / config.get("baseline", "harness.baseline.json")
baseline = (
    json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline_path.exists()
    else None
)
ignore = set(
    config.get(
        "ignore", ["node_modules", ".git", ".venv", "__pycache__", "dist", "build"]
    )
)


def rel(p: Path) -> str:
    return p.resolve().relative_to(root.resolve()).as_posix()


def walk(p: Path):
    if not p.exists() or ignore & set(rel(p).split("/")):
        return
    if p.is_dir():
        for child in sorted(p.iterdir()):
            yield from walk(child)
    else:
        yield rel(p)


files = sorted({f for p in (paths or config["roots"]) for f in walk(root / p)})


def applies(rule, f):
    exts = rule.get("extensions", config.get("extensions", []))
    return (
        any(f.endswith(x) for x in exts)
        and not any(re.search(x, f) for x in rule.get("excludeFiles", []))
        and (
            not rule.get("onlyFiles") or any(re.search(x, f) for x in rule["onlyFiles"])
        )
    )


counts: dict[str, dict[str, int]] = {}
hits: dict[str, list[str]] = {}
frozen: dict[str, str] = {}
for rule in config["rules"]:
    counts[rule["id"]], hits[rule["id"]] = {}, []
    for f in files:
        if rule.get("kind") == "frozen":
            if any(f == d or f.startswith(d.rstrip("/") + "/") for d in rule["files"]):
                frozen[f] = hashlib.sha256((root / f).read_bytes()).hexdigest()
            continue
        if not applies(rule, f):
            continue
        text = (root / f).read_text(encoding="utf-8", errors="replace")
        if rule.get("fileMustMatch") and not re.search(
            rule["fileMustMatch"], text, re.M
        ):
            continue
        # pathCapture : le groupe 1 capturé dans le chemin remplace $1 dans le motif.
        pattern = rule["pattern"]
        if rule.get("pathCapture"):
            cap = re.search(rule["pathCapture"], f)
            if not cap:
                continue
            pattern = pattern.replace("$1", re.escape(cap.group(1)))
        rx = re.compile(pattern, re.I if "i" in rule.get("flags", "") else 0)
        n = 0
        for i, line in enumerate(text.split("\n"), 1):
            found = rx.findall(line)
            if found:
                n += len(found)
                hits[rule["id"]].append(f"{f}:{i}")
        if n:
            counts[rule["id"]][f] = n

if "--update-baseline" in flags:
    nxt: dict[str, dict] = {"ratchet": {}, "frozen": {}}
    raised: list[str] = []
    for rule in (r for r in config["rules"] if r.get("mode", "strict") == "ratchet"):
        nxt["ratchet"][rule["id"]] = counts[rule["id"]]
        for f, n in counts[rule["id"]].items():
            before = ((baseline or {}).get("ratchet", {}).get(rule["id"], {})).get(f, 0)
            if baseline and n > before:
                raised.append(f"{f} [{rule['id']}] {before} → {n}")
    nxt["frozen"] = (
        frozen
        if ("--accept-frozen" in flags or not baseline)
        else baseline.get("frozen", {})
    )
    if raised:
        print(
            "harness: baseline refusée, la dette ne peut que baisser :\n  "
            + "\n  ".join(raised),
            file=sys.stderr,
        )
        sys.exit(2)
    baseline_path.write_text(
        json.dumps(nxt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"harness: baseline écrite ({rel(baseline_path)})")
    sys.exit(0)

failures, debt, can_lower = [], 0, 0
for rule in config["rules"]:
    mode = "frozen" if rule.get("kind") == "frozen" else rule.get("mode", "strict")
    if mode == "frozen":
        ref_all = (baseline or {}).get("frozen", {})
        for f, h in frozen.items():
            if ref_all.get(f) != h:
                failures.append(
                    f"{f} [{rule['id']}] {'modifié' if f in ref_all else 'nouveau'} : {rule['message']}"
                )
        if not paths:
            failures += [
                f"{f} [{rule['id']}] supprimé : {rule['message']}"
                for f in ref_all
                if f not in frozen
            ]
        continue
    for f, n in counts[rule["id"]].items():
        allowed = (
            ((baseline or {}).get("ratchet", {}).get(rule["id"], {})).get(f, 0)
            if mode == "ratchet"
            else 0
        )
        if n > allowed:
            lines = ", ".join(
                [h for h in hits[rule["id"]] if h.startswith(f + ":")][:5]
            )
            failures.append(
                f"{f} [{rule['id']}] {n} occurrence(s), {allowed} tolérée(s) ({lines}) : {rule['message']}"
            )
        elif n < allowed:
            can_lower += 1
        if mode == "ratchet":
            debt += min(n, allowed)

if failures:
    print("\n".join(failures), file=sys.stderr)
    print(
        f"harness: {len(failures)} violation(s) bloquante(s) sur {len(files)} fichier(s)",
        file=sys.stderr,
    )
    sys.exit(1)
tip = (
    f" ; {can_lower} fichier(s) sous la baseline : lancer --update-baseline pour la baisser"
    if can_lower
    else ""
)
print(
    f"harness: OK ({len(config['rules'])} règles, {len(files)} fichiers, dette tolérée {debt}{tip})"
)
