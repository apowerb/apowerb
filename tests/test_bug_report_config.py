"""Le dépôt de destination se configure depuis l'application, pas par l'environnement.

Un noyau open source est déployé par des gens qui ne sont pas ceux qui
l'administrent. Laisser le dépôt des signalements dans une variable
d'environnement obligeait à passer par celui qui déploie pour une décision
qui appartient à l'administrateur du produit.

Deux exigences opposées sont testées ensemble ici :

- **ce qui doit être configurable l'est** — le dépôt et le jeton ;
- **ce qui ne doit PAS l'être ne l'est pas** — l'échappatoire qui désarme
  le refus d'écrire dans un dépôt public.
"""

import pytest

from apowerb.core.config_admin.catalog import (
    BY_NAME,
    InvalidValue,
    normalize,
)


def test_le_depot_et_le_jeton_sont_configurables():
    assert "BUG_REPORT_GITHUB_REPO" in BY_NAME
    assert "BUG_REPORT_GITHUB_TOKEN" in BY_NAME


def test_le_jeton_est_marque_secret_et_le_depot_ne_lest_pas():
    """Un secret n'est jamais réaffiché ; un nom de dépôt peut l'être."""
    assert BY_NAME["BUG_REPORT_GITHUB_TOKEN"].secret is True
    assert BY_NAME["BUG_REPORT_GITHUB_REPO"].secret is False


def test_lechappatoire_vers_un_depot_public_nest_PAS_configurable():
    """La ligne qui compte.

    `BUG_REPORT_GITHUB_ALLOW_PUBLIC` désarme le garde qui refuse d'écrire
    dans un dépôt public. Or un signalement porte des logs serveur, des
    erreurs de navigateur et l'adresse de celui qui l'a envoyé. L'exposer
    dans un écran web offrirait un interrupteur « publier les captures
    d'écran de mes utilisateurs », à deux clics et sans accès au serveur.

    Même raison qui tient `BYPASS_AUTH` hors de ce catalogue.
    """
    assert "BUG_REPORT_GITHUB_ALLOW_PUBLIC" not in BY_NAME


def test_le_depot_est_normalise_avant_stockage():
    """C'est la valeur RENDUE qui part en base : elle doit être propre.

    Le 04/09/2026, un espace final avait passé une garde qui nettoyait
    sans stocker le nettoyé.
    """
    assert normalize("BUG_REPORT_GITHUB_REPO", "  acme/support  ") == "acme/support"


@pytest.mark.parametrize(
    "colle",
    [
        "https://github.com/acme/support",
        "http://github.com/acme/support",
        "git@github.com:acme/support.git",
    ],
)
def test_une_url_collee_depuis_le_navigateur_est_refusee(colle):
    """Le cas le plus probable : on copie la barre d'adresse.

    Le sink construit `/repos/{valeur}/issues` ; une URL y produirait un
    chemin absurde et un 404 le jour de la première publication, loin de
    l'écran où la valeur a été saisie.
    """
    with pytest.raises(InvalidValue) as exc:
        normalize("BUG_REPORT_GITHUB_REPO", colle)
    # Le message doit montrer la forme attendue, pas seulement refuser.
    assert "organisation/dépôt" in str(exc.value)


@pytest.mark.parametrize(
    "invalide",
    ["acme", "acme/", "/support", "a/b/c", "acme/sup port", "acme/sup@port"],
)
def test_les_formes_qui_ne_sont_pas_un_depot_sont_refusees(invalide):
    with pytest.raises(InvalidValue):
        normalize("BUG_REPORT_GITHUB_REPO", invalide)


def test_un_depot_valide_passe():
    for valide in ("acme/support", "thaink2/apowerb", "org-1/repo.name_2"):
        assert normalize("BUG_REPORT_GITHUB_REPO", valide) == valide
