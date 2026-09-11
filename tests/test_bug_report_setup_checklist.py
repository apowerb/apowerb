"""L'administrateur doit DÉCOUVRIR qu'il peut router les signalements.

Sans cette ligne de checklist, on n'apprend l'existence du réglage qu'en
cliquant sur « Créer l'issue » et en tombant sur un refus — c'est-à-dire
au pire moment, en plein triage.

La capacité est `optional=True` : sans dépôt, la fonctionnalité marche
entièrement, les signalements restent dans l'écran de triage. Ce n'est pas
un défaut à corriger, c'est le mode par défaut assumé — et c'est pourquoi
elle ne doit pas gonfler le compteur des configurations manquantes.
"""

from apowerb.core.setup_status import capabilities, setup_status


class _Settings:
    """Les seuls attributs que la capacité lit."""

    def __init__(self, repo="", token=""):
        self.bug_report_github_repo = repo
        self.bug_report_github_token = token

    def __getattr__(self, nom):
        # Les autres capacités lisent leurs propres réglages ; elles ne
        # sont pas le sujet de ce test.
        return ""


def _capacite(settings):
    return {c.key: c for c in capabilities(settings=settings, env={})}["bug_reports"]


def test_la_ligne_existe_meme_sans_configuration():
    """C'est tout son intérêt : signaler une possibilité, pas un manque."""
    c = _capacite(_Settings())
    assert c.optional is True
    assert c.configured is False
    assert c.mode == "in_app"


def test_elle_nomme_les_variables_qui_manquent():
    c = _capacite(_Settings())
    assert c.missing == ["BUG_REPORT_GITHUB_REPO", "BUG_REPORT_GITHUB_TOKEN"]


def test_un_depot_sans_jeton_reste_non_configure():
    """La moitié d'une configuration n'en est pas une."""
    c = _capacite(_Settings(repo="acme/support"))
    assert c.configured is False
    assert c.missing == ["BUG_REPORT_GITHUB_TOKEN"]


def test_les_deux_posees_la_marquent_configuree():
    c = _capacite(_Settings(repo="acme/support", token="xxxx"))
    assert c.configured is True
    assert c.mode == "github"
    assert c.missing == []


def test_elle_ne_gonfle_pas_le_compteur_de_manquants():
    """`optional=True` : une sortie non configurée ne bloque rien."""
    avant = setup_status(for_admin=True, settings=_Settings(), env={})
    apres = setup_status(
        for_admin=True, settings=_Settings(repo="acme/support", token="x"), env={}
    )
    assert avant.missing_count == apres.missing_count


def test_les_noms_de_variables_ne_sont_servis_quaux_administrateurs():
    """Une liste de variables d'environnement renseigne sur l'installation."""
    public = setup_status(for_admin=False, settings=_Settings(), env={})
    ligne = {c.key: c for c in public.items}["bug_reports"]
    assert ligne.missing == []
