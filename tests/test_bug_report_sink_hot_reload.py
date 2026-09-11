"""Une valeur posée depuis l'écran doit agir sans redémarrer le service.

Vécu le 11/09/2026. L'écran d'administration affiche « redémarrez pour
appliquer » ; un administrateur pose le dépôt et le jeton, redémarre — et rien
ne s'applique, parce que l'unité systemd de ce déploiement ne chargeait que son
propre fichier d'environnement. Le correctif de déploiement a réglé ce cas,
mais il laisse le redémarrage obligatoire.

`overlay.py` explique pourquoi le rechargement à chaud est refusé *en général* :
33 modules capturent `Settings` à l'import, et n'en recharger qu'une partie
donnerait un processus à moitié à jour — pire que pas de rechargement du tout.

Le ticketing est précisément l'exception, et c'est mesurable : `build_sink()`
appelle `get_settings()` **dans son corps**, à chaque création d'issue, et rien
dans ce domaine ne capture `Settings` au niveau module. Il n'y a donc aucune
moitié de processus à désynchroniser ici.

La précédence reste celle qu'annonce l'écran : ce que porte le déploiement
gagne, la valeur posée ne comble que ce qui est resté vide.
"""

from apowerb.bug_reports.service import build_sink


class _Settings:
    def __init__(self, **valeurs):
        self.bug_report_github_repo = valeurs.get("repo", "")
        self.bug_report_github_token = valeurs.get("token", "")
        self.bug_report_github_project = valeurs.get("project", "")
        self.bug_report_github_allow_public = False


def test_sans_rien_de_pose_ni_de_deploye_il_n_y_a_pas_de_sortie(monkeypatch):
    monkeypatch.setattr(
        "apowerb.bug_reports.service.get_settings", lambda: _Settings()
    )
    assert build_sink() is None


def test_une_valeur_posee_suffit_sans_redemarrage(monkeypatch):
    monkeypatch.setattr(
        "apowerb.bug_reports.service.get_settings", lambda: _Settings()
    )

    sink = build_sink(
        posed={
            "BUG_REPORT_GITHUB_REPO": "org/prive",
            "BUG_REPORT_GITHUB_TOKEN": "jeton-pose",
        }
    )

    assert sink is not None
    assert sink.repo == "org/prive"


def test_le_deploiement_garde_la_main_sur_ce_qu_il_porte(monkeypatch):
    """La précédence annoncée par l'écran : « imposée par le déploiement »."""
    monkeypatch.setattr(
        "apowerb.bug_reports.service.get_settings",
        lambda: _Settings(repo="org/du-deploiement", token="jeton-du-deploiement"),
    )

    sink = build_sink(posed={"BUG_REPORT_GITHUB_REPO": "org/posee-apres-coup"})

    assert sink.repo == "org/du-deploiement"


def test_le_numero_de_projet_suit_le_meme_chemin(monkeypatch):
    monkeypatch.setattr(
        "apowerb.bug_reports.service.get_settings", lambda: _Settings()
    )

    sink = build_sink(
        posed={
            "BUG_REPORT_GITHUB_REPO": "org/prive",
            "BUG_REPORT_GITHUB_TOKEN": "jeton",
            "BUG_REPORT_GITHUB_PROJECT": "2",
        }
    )

    assert sink._project_number == 2


def test_un_numero_de_projet_illisible_est_ignore_sans_casser(monkeypatch):
    """Une valeur abîmée en base ne doit pas empêcher de créer un ticket."""
    monkeypatch.setattr(
        "apowerb.bug_reports.service.get_settings", lambda: _Settings()
    )

    sink = build_sink(
        posed={
            "BUG_REPORT_GITHUB_REPO": "org/prive",
            "BUG_REPORT_GITHUB_TOKEN": "jeton",
            "BUG_REPORT_GITHUB_PROJECT": "pas-un-nombre",
        }
    )

    assert sink is not None
    assert sink._project_number is None
