"""L'auteur d'un signalement apprend qu'il est clos, quel que soit le chemin.

Question d'un utilisateur le 15/09/2026 : « est-ce que les users sont informés
quand les bugs sont corrigés ? » Il venait de recevoir trois notifications, toutes nées
d'une issue GitHub fermée. Rien ne partait quand un administrateur clôt depuis
l'écran de triage, rien pour un rejet ni un doublon, la notification s'affichait
comme un avertissement et renvoyait vers l'accueil.

Une seule notification par clôture : elle naît du passage d'un état ouvert à un
état clos. Un signalement déjà clos par le triage n'est plus résolu par la
veille, donc ne notifie pas une seconde fois ; rouvert puis reclos, il notifie
de nouveau.
"""

from types import SimpleNamespace

import pytest

from apowerb.bug_reports import tracking


def _rapport(**champs):
    base = dict(id=12, title="Le PDF ne s'indexe pas", status="triaged",
                user_id=7, duplicate_of=None, admin_note=None)
    base.update(champs)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# La décision
# ---------------------------------------------------------------------------


def test_resolu_annonce_la_correction_en_succes():
    avis = tracking.closure_notice(_rapport(), to_status="resolved")
    assert avis.user_id == 7
    assert avis.title == "Votre signalement #12 est corrigé"
    assert "Le PDF ne s'indexe pas" in avis.message
    assert avis.type == "success"


def test_rejete_est_annonce_avec_la_note_de_triage():
    avis = tracking.closure_notice(
        _rapport(admin_note="Comportement attendu : le fichier dépasse 50 Mo."),
        to_status="rejected",
    )
    assert avis.title == "Votre signalement #12 a été examiné"
    assert "le fichier dépasse 50 Mo" in avis.message
    assert avis.type == "info"


def test_rejete_sans_note_ne_montre_pas_de_note_vide():
    avis = tracking.closure_notice(_rapport(admin_note="   "), to_status="rejected")
    assert "Note" not in avis.message


def test_doublon_nomme_le_signalement_deja_suivi():
    avis = tracking.closure_notice(_rapport(duplicate_of=4), to_status="duplicate")
    assert avis.title == "Votre signalement #12 rejoint le #4, déjà suivi"
    assert avis.type == "info"


def test_doublon_sans_reference_reste_lisible():
    avis = tracking.closure_notice(_rapport(), to_status="duplicate")
    assert avis.title == "Votre signalement #12 est déjà suivi"


def test_la_note_n_accompagne_que_le_rejet():
    note = "Note interne de triage"
    for statut in ("resolved", "duplicate"):
        avis = tracking.closure_notice(_rapport(admin_note=note), to_status=statut)
        assert note not in avis.message, statut


@pytest.mark.parametrize("statut", ["new", "triaged", "issue_created"])
def test_un_statut_ouvert_ne_notifie_pas(statut):
    assert tracking.closure_notice(_rapport(), to_status=statut) is None


def test_un_signalement_deja_clos_ne_notifie_pas_deux_fois():
    assert tracking.closure_notice(_rapport(status="resolved"), to_status="rejected") is None


def test_un_signalement_rouvert_puis_reclos_notifie_de_nouveau():
    assert tracking.closure_notice(_rapport(status="new"), to_status="resolved") is not None


def test_un_signalement_anonyme_ne_notifie_personne():
    assert tracking.closure_notice(_rapport(user_id=None), to_status="resolved") is None


def test_l_administrateur_qui_clot_son_propre_signalement_n_est_pas_notifie():
    avis = tracking.closure_notice(_rapport(user_id=9), to_status="resolved", actor_user_id=9)
    assert avis is None


# ---------------------------------------------------------------------------
# L'envoi
# ---------------------------------------------------------------------------


class _SessionNotif:
    def __init__(self, ajoutes):
        self.ajoutes = ajoutes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, objet):
        self.ajoutes.append(objet)

    async def commit(self):
        pass

    async def refresh(self, objet):
        objet.id = 1
        objet.created_at = None


@pytest.mark.asyncio
async def test_la_notification_ne_porte_ni_lien_ni_avertissement(monkeypatch):
    from apowerb.helpers import database, notification_bus

    ajoutes, pousses = [], []
    monkeypatch.setattr(database.sessionmanager, "session", lambda: _SessionNotif(ajoutes))

    async def pousser(user_id, charge):
        pousses.append((user_id, charge))

    monkeypatch.setattr(notification_bus, "notify", pousser)

    avis = tracking.closure_notice(_rapport(), to_status="resolved")
    await tracking.send_notice(avis)

    (notification,) = ajoutes
    assert notification.user_id == 7
    assert notification.link is None
    assert notification.type == "success"
    assert pousses[0][1]["link"] is None


# ---------------------------------------------------------------------------
# Le triage
# ---------------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.ajoutes = []
        self.commits = 0

    def add(self, objet):
        self.ajoutes.append(objet)

    async def commit(self):
        self.commits += 1

    async def refresh(self, objet):
        pass


async def _patch(monkeypatch, rapport, **champs):
    from apowerb.bug_reports import service
    from apowerb.routers import bug_reports as route
    from apowerb.schema.bug_report_schema import BugReportUpdate

    envoyes = []

    async def trouver(_db, _id):
        return rapport

    async def envoyer(avis):
        # Envoyé APRÈS le commit : une notification pour une clôture annulée
        # annoncerait un état qui n'existe pas.
        envoyes.append((avis, db.commits))

    monkeypatch.setattr(service, "get_bug_report", trouver)
    monkeypatch.setattr(service, "to_detail", lambda r: r)
    monkeypatch.setattr(tracking, "send_notice", envoyer)
    db = _Db()
    await route.update_bug_report(
        rapport.id, BugReportUpdate(**champs), db=db, admin=SimpleNamespace(user_id=99)
    )
    return envoyes


@pytest.mark.asyncio
async def test_clore_depuis_le_triage_notifie_l_auteur_apres_le_commit(monkeypatch):
    envoyes = await _patch(monkeypatch, _rapport(), status="rejected", admin_note="Hors périmètre")
    ((avis, commits_avant_envoi),) = envoyes
    assert avis.user_id == 7
    assert avis.title == "Votre signalement #12 a été examiné"
    assert "Hors périmètre" in avis.message
    assert commits_avant_envoi == 1


@pytest.mark.asyncio
async def test_trier_sans_clore_ne_notifie_pas(monkeypatch):
    assert await _patch(monkeypatch, _rapport(status="new"), status="triaged") == []


@pytest.mark.asyncio
async def test_changer_la_severite_d_un_signalement_clos_ne_renotifie_pas(monkeypatch):
    envoyes = await _patch(monkeypatch, _rapport(status="resolved"), severity="minor")
    assert envoyes == []


# ---------------------------------------------------------------------------
# La veille
# ---------------------------------------------------------------------------


def test_la_veille_passe_par_la_meme_decision():
    """La veille notifiait avec son propre texte ; deux textes finissent par
    diverger. Elle doit passer par `closure_notice` et `send_notice`."""
    from pathlib import Path

    source = (Path(tracking.__file__)).read_text()
    corps = source[source.index("async def run_tick("): source.index("async def bug_report_watch_loop(")]
    assert "closure_notice(" in corps
    assert "send_notice(" in corps
    assert "est corrigé" not in corps
