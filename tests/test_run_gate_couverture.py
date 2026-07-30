"""Aucune porte d'entree ne doit executer un agent sans passer les gardes.

Ce test relit les SOURCES plutot que d'exercer un chemin : c'est la seule
maniere d'attraper la porte qu'on ajoutera dans six mois. Le trou d'origine
etait exactement ca -- le chat controlait le quota, les runs planifies et
les webhooks avaient ete ecrits ailleurs et personne ne l'avait remarque,
tous les tests etant verts.
"""
from __future__ import annotations

import ast
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1] / "src" / "th2agent"

# Le module qui DEFINIT les appels sortants vers le serveur ADK : c'est lui
# qu'on appelle, il ne s'appelle pas lui-meme.
DEFINISSEUR = RACINE / "core" / "adk_runner.py"

RUNNERS = {"run_adk_agent", "stream_adk_agent"}
PORTIER = "apply_run_guards"


def _appels(arbre: ast.AST) -> set[str]:
    noms = set()
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.Call):
            continue
        cible = noeud.func
        if isinstance(cible, ast.Name):
            noms.add(cible.id)
        elif isinstance(cible, ast.Attribute):
            noms.add(cible.attr)
    return noms


def _modules_qui_executent_un_agent() -> dict[Path, set[str]]:
    trouves: dict[Path, set[str]] = {}
    for source in RACINE.rglob("*.py"):
        if source == DEFINISSEUR:
            continue
        try:
            arbre = ast.parse(source.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - fichier non parsable
            continue
        appels = _appels(arbre)
        runners_appeles = appels & RUNNERS
        if runners_appeles:
            trouves[source] = appels
    return trouves


def test_toute_porte_passe_par_les_gardes():
    """Tout module qui lance un agent appelle aussi le portier."""
    portes = _modules_qui_executent_un_agent()

    assert portes, (
        "aucun appel a run_adk_agent/stream_adk_agent trouve : le test ne "
        "verifie plus rien, verifier RACINE et les noms des runners"
    )

    sans_garde = sorted(
        str(chemin.relative_to(RACINE))
        for chemin, appels in portes.items()
        if PORTIER not in appels
    )

    assert not sans_garde, (
        "ces modules executent un agent sans passer par "
        f"core.run_gate.{PORTIER} : {sans_garde}. Toute porte d'entree doit "
        "appliquer les gardes, sinon le plafond de quota se contourne en "
        "changeant simplement de chemin d'appel."
    )
