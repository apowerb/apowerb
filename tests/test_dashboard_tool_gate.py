"""Le lecteur de dashboard n'est injecte que pour un agent REELLEMENT lie.

``tool_get_dashboard_data`` etait ajoute a TOUS les agents. Sans dashboard il ne
peut repondre que ``{"error": "No dashboard_id provided ..."}`` -- et le pipeline
de graphiques BI lit alors cette prose comme une donnee et affiche "Aucune donnee"
(apowerb/roadmap#60). Il coutait en plus 1 185 caracteres de schema sur CHAQUE
prompt : mesure du 17/09/2026, les declarations d'outils pesaient 64% d'une requete
de 28 023 caracteres dont la conversation reelle faisait 361 caracteres.
"""
import os

import pytest

from apowerb.core.agent_helpers.agent_utils import _should_inject_dashboard_tool


@pytest.mark.parametrize("valeur", ["", "   ", None])
def test_pas_de_dashboard_lie_pas_d_outil(monkeypatch, valeur):
    """Aucun dashboard lie : l'outil n'a rien a repondre, il n'a rien a faire la."""
    if valeur is None:
        monkeypatch.delenv("AGENT_DASHBOARD_ID", raising=False)
    else:
        monkeypatch.setenv("AGENT_DASHBOARD_ID", valeur)
    assert _should_inject_dashboard_tool() is False


def test_dashboard_lie_outil_present(monkeypatch):
    """Un agent lie a un dashboard garde l'outil : le mini-chat BI continue de marcher."""
    monkeypatch.setenv("AGENT_DASHBOARD_ID", "a4211153-ca13-4888-b34c-114cd8fab6b9")
    assert _should_inject_dashboard_tool() is True


def test_meme_condition_que_l_injection_deja_conditionnee(monkeypatch):
    """``extras_loader.inject_bi_dashboard_tools`` injecte DEJA le meme outil sous
    cette meme condition. Les deux chemins doivent s'accorder, sinon l'un rattrape
    ce que l'autre ecarte et la garde ne sert a rien."""
    from apowerb.core.agent_helpers import extras_loader

    monkeypatch.delenv("AGENT_DASHBOARD_ID", raising=False)
    noms, funcs = [], []
    extras_loader.inject_bi_dashboard_tools("agent-test", noms, funcs, "owner-test")
    assert funcs == [], "extras_loader n'injecte rien sans dashboard"
    assert _should_inject_dashboard_tool() is False, "la garde doit dire la meme chose"
