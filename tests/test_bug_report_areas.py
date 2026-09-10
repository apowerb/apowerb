"""La fonctionnalité concernée : déduite de la route, corrigeable ensuite.

Demander à l'utilisateur de classer son propre bug le fait se tromper :
il nomme la fonctionnalité qu'il croyait utiliser, pas celle qui a
cassé. La route ne se trompe pas — d'où une liste fermée, pré-remplie.
"""

from apowerb.bug_reports.areas import AREA_LABELS, BugArea, area_options, infer_area


def test_le_chemin_de_lappel_prime_sur_lecran_affiche():
    """Un écran de chat qui casse en enregistrant une clé est un bug « modèles ».

    C'est le cas discriminant de toute la fonction : seul le chemin de
    l'appel fautif le dit, l'écran affiché induit en erreur.
    """
    assert infer_area("/api/saved-api-keys", "/chat") is BugArea.MODELS


def test_la_regle_la_plus_specifique_gagne():
    """`/agents/42/tools` est un problème d'outil, pas d'agent."""
    assert infer_area("/agents/42/tools") is BugArea.TOOLS
    assert infer_area("/agents/42") is BugArea.AGENTS


def test_une_route_inconnue_retombe_sur_autre():
    assert infer_area("/quelque-chose-de-neuf") is BugArea.OTHER
    assert infer_area(None, None) is BugArea.OTHER


def test_les_candidats_vides_sont_ignores_sans_masquer_les_suivants():
    assert infer_area(None, "", "/api/rag/index") is BugArea.KNOWLEDGE


def test_chaque_zone_a_un_libelle_lisible():
    """Un identifiant technique dans un menu déroulant fait choisir au hasard."""
    for area in BugArea:
        assert AREA_LABELS.get(area), f"libellé manquant pour {area.value}"
        assert AREA_LABELS[area] != area.value


def test_la_liste_servie_a_linterface_est_complete_et_ordonnee():
    options = area_options()
    assert [option["value"] for option in options] == [area.value for area in BugArea]
    assert all(option["label"] for option in options)
