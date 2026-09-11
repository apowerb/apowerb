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


# ---------------------------------------------------------------------------
# Type de l'issue et couleur des étiquettes
#
# Vécu le 11/09/2026 sur `apowerb/roadmap#26`, le premier signalement vraiment
# publié : l'issue est arrivée sans **type**, alors que le tableau de David
# filtre là-dessus — un bug sans type n'y apparaît pas. Et les trois étiquettes
# que ce module nomme (`severity:…`, `area:…`, `from:app`) n'existaient pas
# dans le dépôt : GitHub les a créées d'office, toutes en gris `#ededed`, y
# compris `severity:blocker`. Le code couleur du dépôt était perdu.
# ---------------------------------------------------------------------------


class SessionParChemin:
    """Doublure qui répond selon l'URL, et retient ce qu'on lui a envoyé."""

    def __init__(self, *, labels_existants=(), depot_prive=True):
        self.labels_existants = list(labels_existants)
        self.depot_prive = depot_prive
        self.posts = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url.endswith("/labels"):
            return FakeResponse(
                200, [{"name": n, "color": "112233"} for n in self.labels_existants]
            )
        return FakeResponse(200, {"private": self.depot_prive, "visibility": "private"})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/labels"):
            nom = (kwargs.get("json") or {}).get("name")
            self.labels_existants.append(nom)
            return FakeResponse(201, {"name": nom})
        return FakeResponse(201, {"html_url": "https://github.com/org/depot/issues/7",
                                  "number": 7, "node_id": "I_node7"})

    def _posts_vers(self, suffixe):
        return [k for u, k in self.posts if u.endswith(suffixe)]


def test_l_issue_est_creee_avec_son_type():
    session = SessionParChemin(labels_existants=["bug"])
    sink = _sink(session)

    sink.create_issue(title="T", body="B", labels=["bug"], issue_type="Bug")

    (creation,) = session._posts_vers("/issues")
    assert creation["json"]["type"] == "Bug"


def test_une_etiquette_absente_est_creee_avec_une_couleur_choisie():
    session = SessionParChemin(labels_existants=["bug"])
    sink = _sink(session)

    sink.create_issue(
        title="T", body="B", labels=["bug", "severity:blocker", "from:app"]
    )

    crees = {k["json"]["name"]: k["json"]["color"] for k in session._posts_vers("/labels")}
    assert set(crees) == {"severity:blocker", "from:app"}
    # Une couleur choisie, pas celle que GitHub tire au sort.
    assert crees["severity:blocker"] == "B60205"
    assert all(c and c != "ededed" for c in crees.values())


def test_une_etiquette_deja_presente_nest_pas_recreee():
    """Sa couleur appartient au dépôt : la réécrire effacerait un choix."""
    session = SessionParChemin(
        labels_existants=["bug", "severity:blocker", "area:chat", "from:app"]
    )
    sink = _sink(session)

    sink.create_issue(
        title="T", body="B", labels=["bug", "severity:blocker", "area:chat", "from:app"]
    )

    assert session._posts_vers("/labels") == []


def test_une_etiquette_inconnue_du_bareme_reste_posee():
    """On ne refuse pas une étiquette faute de couleur prévue."""
    session = SessionParChemin(labels_existants=[])
    sink = _sink(session)

    sink.create_issue(title="T", body="B", labels=["quelque-chose-de-neuf"])

    (creation,) = session._posts_vers("/issues")
    assert "quelque-chose-de-neuf" in creation["json"]["labels"]


def test_un_echec_de_creation_d_etiquette_ne_bloque_pas_l_issue():
    """Le ticket vaut mieux que sa couleur."""

    class SessionQuiRefuseLesLabels(SessionParChemin):
        def post(self, url, **kwargs):
            if url.endswith("/labels"):
                self.posts.append((url, kwargs))
                return FakeResponse(403, {}, text="pas le droit")
            return super().post(url, **kwargs)

    session = SessionQuiRefuseLesLabels(labels_existants=[])
    sink = _sink(session)

    cree = sink.create_issue(title="T", body="B", labels=["bug", "from:app"])

    assert cree["number"] == 7


# ---------------------------------------------------------------------------
# Rattachement au tableau de projet
#
# Mesuré le 11/09/2026 sur `apowerb/roadmap` : les sept dernières issues ont
# toutes rejoint le Project 2, et à chaque fois par un ajout manuel — l'auteur
# de l'événement `added_to_project_v2` est une personne, jamais
# `github-project-automation[bot]`. Le projet automatise le statut, pas
# l'entrée. Un ticket créé par ce module resterait donc hors du tableau.
# ---------------------------------------------------------------------------


class SessionAvecProjet(SessionParChemin):
    """Ajoute à la doublure précédente les deux appels GraphQL du projet."""

    def __init__(self, *, projet_id="PVT_test", echoue=False, **kwargs):
        super().__init__(**kwargs)
        self.projet_id = projet_id
        self.echoue = echoue
        self.mutations = []

    def post(self, url, **kwargs):
        if url.endswith("/graphql"):
            self.posts.append((url, kwargs))
            requete = (kwargs.get("json") or {}).get("query", "")
            if self.echoue:
                return FakeResponse(200, {"errors": [{"message": "jeton sans droit"}]})
            if "addProjectV2ItemById" in requete:
                self.mutations.append(kwargs["json"].get("variables"))
                return FakeResponse(200, {"data": {"addProjectV2ItemById":
                                                   {"item": {"id": "PVTI_1"}}}})
            return FakeResponse(
                200, {"data": {"organization": {"projectV2": {"id": self.projet_id}}}}
            )
        return super().post(url, **kwargs)


def test_l_issue_rejoint_le_tableau_quand_un_projet_est_configure():
    session = SessionAvecProjet(labels_existants=["bug"])
    sink = _sink(session, project_number=2)

    sink.create_issue(title="T", body="B", labels=["bug"])

    assert session.mutations, "aucune mutation d'ajout au projet"
    variables = session.mutations[0]
    assert variables["projectId"] == "PVT_test"
    assert variables["contentId"] == "I_node7"


def test_sans_projet_configure_aucun_appel_graphql():
    session = SessionAvecProjet(labels_existants=["bug"])
    sink = _sink(session)

    sink.create_issue(title="T", body="B", labels=["bug"])

    assert session._posts_vers("/graphql") == []


def test_un_projet_inaccessible_ne_fait_pas_echouer_le_ticket():
    """Le jeton peut ne pas porter le droit `project` : le ticket reste créé."""
    session = SessionAvecProjet(labels_existants=["bug"], echoue=True)
    sink = _sink(session, project_number=2)

    cree = sink.create_issue(title="T", body="B", labels=["bug"])

    assert cree["number"] == 7
