"""Traçabilité des signalements, suivi des issues fermées, alerte des retards.

Demande d'Elom du 14/09/2026 : savoir ce qui est arrivé à chaque signalement,
et être prévenu quand un bug attend depuis plus de deux jours sans être corrigé.

Décisions verrouillées ce jour-là :

- **corrigé** = résolu, rejeté ou doublon ;
- l'alerte part à **48 h**, puis **une fois par jour** tant que rien ne bouge ;
- destinataires : les superadministrateurs, dans l'application et par e-mail.

Une conséquence n'est pas négociable : un signalement transmis en issue GitHub
n'a, jusqu'ici, aucun moyen de passer « résolu » quand l'issue est fermée. Sans
suivi, il resterait « non corrigé » pour toujours, et l'alerte sonnerait sans
fin sur des bugs réglés depuis longtemps. Le suivi est donc la moitié de
l'alerte, pas une option.

La décision est isolée en fonctions pures : ce qui doit être résolu, ce qui doit
être alerté, ce qui a changé lors d'une mise à jour. Les entrées-sorties (base,
GitHub, e-mail) ne font qu'exécuter ce plan.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from apowerb.bug_reports import tracking

MAINTENANT = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _rapport(**champs):
    base = dict(
        id=1,
        title="Le PDF ne s'indexe pas",
        status="new",
        severity="major",
        created_at=MAINTENANT - timedelta(hours=72),
        issue_number=None,
        user_id=None,
    )
    base.update(champs)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Le retard
# ---------------------------------------------------------------------------


def test_un_bug_de_47_heures_n_est_pas_en_retard():
    r = _rapport(created_at=MAINTENANT - timedelta(hours=47))
    assert tracking.is_overdue(r, last_alert_at=None, now=MAINTENANT) is False


def test_un_bug_de_49_heures_est_en_retard():
    r = _rapport(created_at=MAINTENANT - timedelta(hours=49))
    assert tracking.is_overdue(r, last_alert_at=None, now=MAINTENANT) is True


def test_un_bug_corrige_n_est_jamais_en_retard():
    for statut in ("resolved", "rejected", "duplicate"):
        r = _rapport(status=statut, created_at=MAINTENANT - timedelta(days=30))
        assert tracking.is_overdue(r, last_alert_at=None, now=MAINTENANT) is False, statut


def test_un_bug_trie_ou_transmis_reste_non_corrige():
    """Trier ou créer une issue n'est pas corriger."""
    for statut in ("new", "triaged", "issue_created"):
        r = _rapport(status=statut)
        assert tracking.is_overdue(r, last_alert_at=None, now=MAINTENANT) is True, statut


def test_on_ne_relance_pas_avant_vingt_quatre_heures():
    r = _rapport()
    assert tracking.is_overdue(
        r, last_alert_at=MAINTENANT - timedelta(hours=10), now=MAINTENANT
    ) is False


def test_on_relance_une_fois_par_jour():
    r = _rapport()
    assert tracking.is_overdue(
        r, last_alert_at=MAINTENANT - timedelta(hours=25), now=MAINTENANT
    ) is True


def test_une_date_naive_est_comprise_comme_utc():
    """La colonne est `TIMESTAMPTZ`, mais une doublure ou un pilote peut rendre
    une date sans fuseau : comparer naïf et conscient lève en Python."""
    r = _rapport(created_at=(MAINTENANT - timedelta(hours=49)).replace(tzinfo=None))
    assert tracking.is_overdue(r, last_alert_at=None, now=MAINTENANT) is True


# ---------------------------------------------------------------------------
# Le plan d'une passe
# ---------------------------------------------------------------------------


def test_une_issue_fermee_resout_le_signalement():
    r = _rapport(status="issue_created", issue_number=42)

    plan = tracking.plan_tick(
        [(r, None)], issue_states={42: "closed"}, now=MAINTENANT
    )

    assert plan.to_resolve == [r]
    assert plan.to_alert == []


def test_un_signalement_resolu_dans_la_passe_n_est_pas_alerte():
    """Sinon on préviendrait d'un retard sur un bug qu'on vient de fermer."""
    r = _rapport(status="issue_created", issue_number=42)

    plan = tracking.plan_tick([(r, None)], issue_states={42: "closed"}, now=MAINTENANT)

    assert r not in plan.to_alert


def test_une_issue_ouverte_ne_change_rien_mais_le_retard_compte():
    r = _rapport(status="issue_created", issue_number=42)

    plan = tracking.plan_tick([(r, None)], issue_states={42: "open"}, now=MAINTENANT)

    assert plan.to_resolve == []
    assert plan.to_alert == [r]


def test_un_etat_d_issue_inconnu_ne_resout_rien():
    """GitHub indisponible : on ne déclare pas un bug corrigé faute de réponse."""
    r = _rapport(status="issue_created", issue_number=42)

    plan = tracking.plan_tick([(r, None)], issue_states={}, now=MAINTENANT)

    assert plan.to_resolve == []


def test_les_retards_sont_tries_du_plus_ancien_au_plus_recent():
    vieux = _rapport(id=1, created_at=MAINTENANT - timedelta(days=9))
    moyen = _rapport(id=2, created_at=MAINTENANT - timedelta(days=4))

    plan = tracking.plan_tick([(moyen, None), (vieux, None)], issue_states={}, now=MAINTENANT)

    assert [r.id for r in plan.to_alert] == [1, 2]


# ---------------------------------------------------------------------------
# Le récapitulatif
# ---------------------------------------------------------------------------


def test_le_recapitulatif_dit_combien_et_lesquels():
    a = _rapport(id=3, title="Le PDF ne s'indexe pas", severity="blocker",
                 created_at=MAINTENANT - timedelta(days=5))
    b = _rapport(id=7, title="Avatar qui plante", created_at=MAINTENANT - timedelta(days=3))

    titre, corps = tracking.build_overdue_digest([a, b], now=MAINTENANT)

    assert "2" in titre
    assert "#3" in corps and "Le PDF ne s'indexe pas" in corps
    assert "#7" in corps and "Avatar qui plante" in corps
    assert "5 j" in corps


def test_un_seul_retard_s_ecrit_au_singulier():
    titre, _ = tracking.build_overdue_digest([_rapport()], now=MAINTENANT)
    assert "1 signalement" in titre
    assert "signalements" not in titre


# ---------------------------------------------------------------------------
# Le journal
# ---------------------------------------------------------------------------


def test_une_mise_a_jour_trace_ce_qui_a_change():
    r = _rapport(status="new", severity="major", admin_note=None, area="chat")

    evenements = tracking.diff_update(
        r, status="triaged", severity="blocker", area="chat", admin_note="Vu, repro OK"
    )

    genres = {(e.kind, e.from_value, e.to_value) for e in evenements}
    assert ("status_changed", "new", "triaged") in genres
    assert ("severity_changed", "major", "blocker") in genres
    assert any(e.kind == "note_changed" for e in evenements)
    # La zone n'a pas bougé : rien à tracer.
    assert not any(e.kind == "area_changed" for e in evenements)


def test_une_mise_a_jour_sans_changement_ne_trace_rien():
    r = _rapport(status="triaged", severity="major", admin_note="x", area="chat")

    assert tracking.diff_update(
        r, status="triaged", severity="major", area="chat", admin_note="x"
    ) == []


def test_le_contenu_de_la_note_ne_part_pas_dans_le_journal():
    """Une note d'administrateur peut contenir n'importe quoi ; le journal dit
    qu'elle a changé, pas ce qu'elle dit — il est lu par d'autres."""
    r = _rapport(admin_note=None)

    (evenement,) = tracking.diff_update(r, admin_note="mot de passe du client : …")

    assert evenement.kind == "note_changed"
    assert evenement.from_value is None and evenement.to_value is None


# ---------------------------------------------------------------------------
# Branchements : ce que les fonctions pures ne prouvent pas
# ---------------------------------------------------------------------------

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "apowerb"


class _Reponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Session:
    def __init__(self, reponse=None, leve=False):
        self.reponse = reponse
        self.leve = leve
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append(url)
        if self.leve:
            raise ConnectionError("réseau coupé")
        return self.reponse


def _sink(session):
    from apowerb.bug_reports.sinks.github import GitHubIssueSink

    return GitHubIssueSink(repo="org/prive", token="jeton-de-test", session=session)


@pytest.mark.parametrize("etat", ["open", "closed"])
def test_le_sink_lit_l_etat_d_une_issue(etat):
    session = _Session(_Reponse(200, {"state": etat}))

    assert _sink(session).get_issue_state(42) == etat
    assert session.gets[0].endswith("/repos/org/prive/issues/42")


def test_une_issue_introuvable_n_a_pas_d_etat():
    assert _sink(_Session(_Reponse(404))).get_issue_state(42) is None


def test_une_panne_reseau_n_a_pas_d_etat_et_ne_leve_pas():
    """La veille tourne en tâche de fond : elle ne doit jamais mourir d'un réseau."""
    assert _sink(_Session(leve=True)).get_issue_state(42) is None


def test_la_table_du_journal_est_creee_au_demarrage():
    sql = (SRC / "helpers" / "bug_report_migration.py").read_text()
    assert "bug_report_events" in sql
    assert "ON DELETE CASCADE" in sql


def test_la_veille_est_lancee_au_demarrage():
    """Une boucle écrite mais jamais planifiée ne prévient personne — et rien
    ne le montrerait : aucun test ne l'appellerait, aucune erreur ne sortirait."""
    main = (SRC / "main.py").read_text()
    assert "bug_report_watch_loop" in main
    assert "asyncio.create_task(bug_report_watch_loop())" in main


def test_le_journal_est_servi_par_l_api():
    from apowerb.routers.bug_reports import router

    chemins = {route.path for route in router.routes}
    assert any(p.endswith("/{report_id}/events") for p in chemins), chemins


def test_la_mise_a_jour_passe_par_le_journal():
    """La route PATCH doit tracer ce qu'elle change, pas seulement l'appliquer."""
    route = (SRC / "routers" / "bug_reports.py").read_text()
    debut = route.index("async def update_bug_report(")
    corps = route[debut: route.index("\n@router", debut)]
    assert "diff_update" in corps
    assert "record_events" in corps


def test_la_creation_d_issue_passe_par_le_journal():
    service = (SRC / "bug_reports" / "service.py").read_text()
    debut = service.index("async def create_issue_for(")
    corps = service[debut:]
    assert "EVENT_ISSUE_CREATED" in corps


def test_la_creation_d_un_signalement_passe_par_le_journal():
    service = (SRC / "bug_reports" / "service.py").read_text()
    debut = service.index("async def create_bug_report(")
    corps = service[debut: service.index("\ndef _server_version", debut)]
    assert "EVENT_CREATED" in corps
    assert "EVENT_DUPLICATE_RECEIVED" in corps
