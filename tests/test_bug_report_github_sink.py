"""Le garde qui refuse d'écrire un signalement dans un dépôt public.

C'est le test le plus important de la fonctionnalité. Un corps d'issue
produit ici contient les logs serveur de la requête fautive, les erreurs
du navigateur et l'adresse de celui qui a signalé. Publié, c'est indexé
dans l'heure, et supprimer l'issue ne rattrape ni les notifications
parties ni les caches.

Le mode d'échec est vécu : le 20/08/2026, des documents clients réels se
sont retrouvés dans un dépôt public parce que la question n'avait été
posée à personne au moment d'écrire le code.

Aucun de ces tests ne touche le réseau : c'est le garde qu'on éprouve,
pas la bibliothèque HTTP.
"""

import pytest

from apowerb.bug_reports.sinks.github import (
    GitHubIssueSink,
    SinkConfigurationError,
    SinkDeliveryError,
    SinkRefusal,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Enregistre les appels et rejoue des réponses préparées."""

    def __init__(self, get_response=None, post_response=None):
        self.get_response = get_response or FakeResponse()
        self.post_response = post_response or FakeResponse(201, {})
        self.posts = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_response

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_response


def _sink(session, **kwargs):
    return GitHubIssueSink(
        repo="org/depot", token="xxxx-jeton-de-test", session=session, **kwargs
    )


def test_un_depot_public_est_refuse_et_rien_nest_poste():
    """L'assertion qui compte : le refus arrive AVANT le POST."""
    session = FakeSession(
        get_response=FakeResponse(200, {"private": False, "visibility": "public"})
    )
    sink = _sink(session)

    with pytest.raises(SinkRefusal) as refus:
        sink.create_issue(title="t", body="b")

    assert session.posts == [], "aucune écriture ne doit partir vers un dépôt public"
    message = str(refus.value)
    # Le message doit porter le remède, pas seulement le verdict : c'est
    # un administrateur qui le lira, au moment où il essaie d'agir.
    assert "privé" in message and "BUG_REPORT_GITHUB_ALLOW_PUBLIC" in message


def test_un_depot_prive_laisse_passer():
    session = FakeSession(
        get_response=FakeResponse(200, {"private": True, "visibility": "private"}),
        post_response=FakeResponse(
            201, {"html_url": "https://github.com/org/depot/issues/7", "number": 7}
        ),
    )
    created = _sink(session).create_issue(title="t", body="b", labels=["bug"])
    assert created["number"] == 7
    assert len(session.posts) == 1


def test_visibility_public_gagne_sur_private_true():
    """`private: true` avec `visibility: public` est contradictoire.

    GitHub ne devrait pas produire ce couple, mais le garde tranche dans
    le sens sûr plutôt que de faire confiance au champ historique.
    """
    session = FakeSession(
        get_response=FakeResponse(200, {"private": True, "visibility": "public"})
    )
    with pytest.raises(SinkRefusal):
        _sink(session).create_issue(title="t", body="b")
    assert session.posts == []


def test_lechappatoire_explicite_autorise_le_depot_public():
    """Elle existe pour une démo sans donnée client. Jamais le défaut."""
    session = FakeSession(
        get_response=FakeResponse(200, {"private": False, "visibility": "public"}),
        post_response=FakeResponse(
            201, {"html_url": "https://github.com/org/depot/issues/1", "number": 1}
        ),
    )
    created = _sink(session, allow_public_repo=True).create_issue(title="t", body="b")
    assert created["number"] == 1


def test_le_commentaire_passe_aussi_par_le_garde():
    """Commenter publie autant que créer — même contrôle, même refus."""
    session = FakeSession(
        get_response=FakeResponse(200, {"private": False, "visibility": "public"})
    )
    with pytest.raises(SinkRefusal):
        _sink(session).comment_on_issue(7, "corps")
    assert session.posts == []


def test_la_visibilite_est_relue_a_chaque_ecriture():
    """Pas de cache : un dépôt s'ouvre au public d'un clic.

    Deux créations = deux lectures de la visibilité. Mettre ce contrôle
    en cache reviendrait à faire confiance à une observation périmée.
    """
    session = FakeSession(
        get_response=FakeResponse(200, {"private": True, "visibility": "private"}),
        post_response=FakeResponse(201, {"html_url": "u", "number": 1}),
    )
    sink = _sink(session)
    sink.create_issue(title="a", body="b")
    sink.create_issue(title="c", body="d")
    repo_reads = [url for url, _ in session.gets if url.endswith("/repos/org/depot")]
    assert len(repo_reads) == 2


def test_un_404_est_une_erreur_de_configuration_pas_une_panne():
    """Dépôt absent ou jeton sans accès : GitHub répond 404 pour les deux."""
    session = FakeSession(get_response=FakeResponse(404, {}))
    with pytest.raises(SinkConfigurationError):
        _sink(session).create_issue(title="t", body="b")


def test_un_refus_decriture_de_github_remonte_comme_livraison_ratee():
    session = FakeSession(
        get_response=FakeResponse(200, {"private": True, "visibility": "private"}),
        post_response=FakeResponse(422, {}, text="Validation Failed"),
    )
    with pytest.raises(SinkDeliveryError):
        _sink(session).create_issue(title="t", body="b")


@pytest.mark.parametrize("repo", ["", "sans-slash", "trop/de/segments", "org/"])
def test_un_depot_mal_ecrit_est_refuse_a_la_construction(repo):
    with pytest.raises(SinkConfigurationError):
        GitHubIssueSink(repo=repo, token="xxxx", session=FakeSession())


def test_un_jeton_vide_est_refuse_a_la_construction():
    with pytest.raises(SinkConfigurationError):
        GitHubIssueSink(repo="org/depot", token="", session=FakeSession())


def test_la_recherche_de_doublon_ne_bloque_pas_quand_elle_echoue():
    """Une recherche en panne doit produire un doublon, pas une erreur.

    Un doublon se referme d'un clic ; un signalement perdu ne revient pas.
    """
    session = FakeSession(get_response=FakeResponse(503, {}))
    assert _sink(session).find_existing_issue("ab12cd34") is None
