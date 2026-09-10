"""Vingt témoins d'un défaut doivent produire un ticket, pas vingt.

L'empreinte est la seule chose qui distingue « ce bug est massif » de
« nous avons beaucoup de tickets ». Ces tests l'éprouvent dans les deux
sens : elle doit rapprocher ce qui est le même défaut, et séparer ce qui
n'en est pas un — un regroupement trop large est pire que pas de
regroupement, il cache un bug derrière un autre.
"""

from apowerb.bug_reports.fingerprint import (
    compute_fingerprint,
    normalise_error,
    normalise_route,
)


def test_les_identifiants_de_la_route_sont_neutralises():
    assert normalise_route("/api/agents/42/runs") == "/api/agents/{id}/runs"
    assert (
        normalise_route("/chat/9f1c2d3e-1111-2222-3333-444455556666")
        == "/chat/{id}"
    )
    assert normalise_route("/agents/agent201/tools") == "/agents/agent{id}/tools"


def test_la_query_string_ne_fait_pas_partie_de_la_route():
    assert normalise_route("/api/runs?page=3") == normalise_route("/api/runs?page=9")


def test_deux_occurrences_du_meme_defaut_se_rejoignent():
    """Agents différents, durées différentes : même chemin de code."""
    a = compute_fingerprint(
        route="/api/agents/42/runs", status=500, error_signature="Timeout after 30000 ms"
    )
    b = compute_fingerprint(
        route="/api/agents/77/runs", status=500, error_signature="Timeout after 45000 ms"
    )
    assert a == b


def test_deux_defauts_distincts_ne_se_confondent_pas():
    """Même route, statuts différents : deux problèmes, deux tickets."""
    interdit = compute_fingerprint(route="/api/agents/1", status=403, error_signature="Forbidden")
    casse = compute_fingerprint(route="/api/agents/1", status=500, error_signature="Forbidden")
    assert interdit != casse


def test_une_route_differente_separe_meme_avec_la_meme_erreur():
    a = compute_fingerprint(route="/api/tools", status=500, error_signature="boom")
    b = compute_fingerprint(route="/api/skills", status=500, error_signature="boom")
    assert a != b


def test_seule_la_premiere_ligne_de_lerreur_compte():
    """La pile change d'une occurrence à l'autre sans que le bug change."""
    court = compute_fingerprint(route="/x", status=500, error_signature="TypeError: nope")
    long = compute_fingerprint(
        route="/x", status=500, error_signature="TypeError: nope\n  at f (a.js:12)\n  at g"
    )
    assert court == long


def test_les_chemins_absolus_et_les_adresses_memoire_sont_du_bruit():
    assert normalise_error("failed at /srv/app/src/thing.py line 42") == normalise_error(
        "failed at /opt/other/place/thing.py line 87"
    )


def test_un_signalement_purement_narratif_a_quand_meme_une_empreinte():
    """« Le bouton ne fait rien » : ni statut, ni erreur, mais une route."""
    empreinte = compute_fingerprint(route="/agents/12", status=None, error_signature=None)
    assert len(empreinte) == 16
    assert empreinte == compute_fingerprint(
        route="/agents/99", status=None, error_signature=""
    )


def test_lempreinte_est_courte_et_stable():
    args = dict(route="/a", status=500, error_signature="x")
    assert compute_fingerprint(**args) == compute_fingerprint(**args)
    assert len(compute_fingerprint(**args)) == 16
