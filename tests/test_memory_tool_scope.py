"""L'outil memory du catalogue range chaque souvenir par agent ET par utilisateur.

Avant ce correctif (apowerb/roadmap#84), les outils exposés au catalogue n'étaient liés
à rien : leur dossier venait de la variable de process ``AGENT_FOLDER``, que rien
ne posait par exécution. Tous les agents et tous les utilisateurs de l'instance
écrivaient et lisaient le même ``uploads/default/.agent_memory.json``.

Chaque test pose une valeur reconnaissable et vérifie qui peut la relire,
plutôt que de vérifier qu'une fonction a été appelée.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apowerb.tools_store.portfolio import memory


def _ctx(app_name: str, user_id: str):
    """Le strict nécessaire d'un ToolContext ADK : l'agent racine et l'appelant."""
    return SimpleNamespace(user_id=user_id, session=SimpleNamespace(app_name=app_name))


@pytest.fixture()
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "uploads_dir", lambda: tmp_path)
    monkeypatch.delenv("AGENT_FOLDER", raising=False)
    return tmp_path


# Ce que le catalogue sert réellement à un agent : l'attribut du module.
SAVE = memory.tool_save_memory
SEARCH = memory.tool_search_memory
SAVE_USAGE = memory.tool_save_question_tool_usage
SEARCH_USAGE = memory.tool_search_saved_tool_usages


def test_un_utilisateur_ne_relit_pas_la_memoire_d_un_autre(uploads):
    SAVE("IBAN client Dupont FR76 1234", tool_context=_ctx("agent1", "alice@ex.com"))

    autre = SEARCH("IBAN Dupont", tool_context=_ctx("agent1", "bob@ex.com"))
    soi = SEARCH("IBAN Dupont", tool_context=_ctx("agent1", "alice@ex.com"))

    assert autre["result_count"] == 0
    assert soi["result_count"] == 1


def test_un_agent_ne_relit_pas_la_memoire_d_un_autre_agent(uploads):
    SAVE("regle metier confidentielle", tool_context=_ctx("agent1", "alice@ex.com"))

    assert SEARCH("confidentielle", tool_context=_ctx("agent2", "alice@ex.com"))["result_count"] == 0


def test_les_motifs_d_usage_d_outil_sont_cloisonnes_aussi(uploads):
    SAVE_USAGE("ventes du mois", "sql.tool_query", {"q": "select 1"},
               tool_context=_ctx("agent1", "alice@ex.com"))

    assert SEARCH_USAGE("ventes", tool_context=_ctx("agent1", "bob@ex.com"))["result_count"] == 0
    assert SEARCH_USAGE("ventes", tool_context=_ctx("agent1", "alice@ex.com"))["result_count"] == 1


def test_sans_appelant_connu_l_outil_refuse_au_lieu_de_partager(uploads):
    res = SAVE("valeur orpheline")

    assert res["success"] is False
    assert list(uploads.rglob("*.json")) == []
    assert not (uploads / "default").exists()


def test_la_variable_de_process_ne_redirige_plus_la_memoire(uploads, monkeypatch):
    # index_db.py pose AGENT_FOLDER pour tout le process : il ne doit plus
    # décider où tombe la mémoire d'un autre appelant.
    monkeypatch.setenv("AGENT_FOLDER", "session-de-quelqu-un-d-autre")

    SAVE("souvenir de alice", tool_context=_ctx("agent1", "alice@ex.com"))

    assert not (uploads / "session-de-quelqu-un-d-autre").exists()


def test_un_identifiant_hostile_ne_sort_pas_du_dossier_uploads(uploads):
    SAVE("x", tool_context=_ctx("agent1", "../../../../tmp/evasion"))

    fichiers = list(uploads.rglob(".agent_memory.json"))
    assert len(fichiers) == 1
    assert fichiers[0].resolve().is_relative_to(uploads.resolve())


def test_un_nom_d_agent_hostile_est_refuse(uploads):
    res = SAVE("x", tool_context=_ctx("../escape", "alice@ex.com"))

    assert res["success"] is False
    assert list(uploads.parent.rglob("escape")) == []


def test_le_modele_ne_voit_pas_le_parametre_tool_context():
    from google.adk.tools import FunctionTool

    for fn in (SAVE, SEARCH, SAVE_USAGE, SEARCH_USAGE):
        decl = FunctionTool(fn)._get_declaration()
        props = decl.parameters.properties if decl.parameters else {}
        assert "tool_context" not in props, fn.__name__
