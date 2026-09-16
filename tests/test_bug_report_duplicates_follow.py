"""Un doublon ne reste pas ouvert quand son signalement d'origine est clos.

Vu en dev le 16/09/2026 : deux signalements affichés « Issue créée » alors
que leur issue GitHub était fermée depuis deux jours — #4 et #8, doublons de
#2 et #7, tous deux résolus. La veille exclut les doublons de sa sélection,
pour ne pas alerter deux fois le même défaut ; elle ne les résolvait donc
jamais. À l'écran de triage, ils restaient « en attente » pour toujours.

La clôture du canonique — par la veille comme par le triage — se propage
désormais à ses doublons : même statut, une trace dans leur journal, et une
notification à leur auteur, qui n'a pas à savoir que son envoi a été
rattaché à un autre.
"""

from types import SimpleNamespace

import pytest

from apowerb.bug_reports import tracking


def _rapport(**champs):
    base = dict(id=2, title="Le PDF ne s'indexe pas", status="issue_created",
                user_id=7, duplicate_of=None, admin_note=None)
    base.update(champs)
    return SimpleNamespace(**base)


class _Resultat:
    def __init__(self, lignes):
        self._lignes = lignes

    def scalars(self):
        return self._lignes

    def scalar_one_or_none(self):
        return self._lignes[0] if self._lignes else None


class _Db:
    """Rend les doublons demandés, et note ce qui est ajouté."""

    def __init__(self, doublons=()):
        self.doublons = list(doublons)
        self.ajoutes = []
        self.requetes = []

    async def execute(self, statement=None, *_args, **_kwargs):
        # La doublure rend ce qu'on lui a donné : c'est la REQUÊTE qu'il faut
        # regarder pour savoir qui serait vraiment ramené en base.
        self.requetes.append(statement)
        return _Resultat(self.doublons)

    def add(self, objet):
        self.ajoutes.append(objet)

    async def commit(self):
        pass

    async def refresh(self, _objet):
        pass


@pytest.mark.asyncio
async def test_les_doublons_prennent_le_statut_du_canonique():
    canonique = _rapport(id=2)
    doublon = _rapport(id=4, user_id=9, duplicate_of=2)
    db = _Db([doublon])

    avis = await tracking.close_with_duplicates(db, canonique, "resolved")

    assert canonique.status == "resolved"
    assert doublon.status == "resolved"
    assert [a.user_id for a in avis] == [7, 9]


@pytest.mark.asyncio
async def test_la_requete_cherche_les_doublons_de_CE_signalement():
    """Une doublure rend ce qu'on lui donne : sans lire la requête, un filtre
    faux passerait inaperçu."""
    canonique = _rapport(id=2)
    db = _Db([])

    await tracking.close_with_duplicates(db, canonique, "resolved")

    (requete,) = db.requetes
    compilee = requete.compile()
    assert "duplicate_of" in str(compilee)
    assert 2 in compilee.params.values(), compilee.params


@pytest.mark.asyncio
async def test_le_journal_du_doublon_dit_qu_il_suit_son_canonique():
    canonique = _rapport(id=2)
    doublon = _rapport(id=4, user_id=9, duplicate_of=2)
    db = _Db([doublon])

    await tracking.close_with_duplicates(db, canonique, "resolved")

    traces = [e for e in db.ajoutes if e.bug_report_id == 4]
    assert traces, "le doublon doit garder une trace de sa clôture"
    assert traces[0].kind == tracking.EVENT_STATUS_CHANGED
    assert traces[0].to_value == "resolved"
    assert "#2" in (traces[0].detail or ""), traces[0].detail


@pytest.mark.asyncio
async def test_un_doublon_deja_clos_n_est_pas_reclos():
    canonique = _rapport(id=2)
    doublon = _rapport(id=4, user_id=9, duplicate_of=2, status="rejected")
    db = _Db([doublon])

    avis = await tracking.close_with_duplicates(db, canonique, "resolved")

    assert doublon.status == "rejected"
    assert [a.user_id for a in avis] == [7]


@pytest.mark.asyncio
async def test_sans_doublon_rien_de_plus_ne_se_passe():
    canonique = _rapport(id=2)
    db = _Db([])

    avis = await tracking.close_with_duplicates(db, canonique, "rejected")

    assert canonique.status == "rejected"
    assert [a.user_id for a in avis] == [7]
    assert [e.bug_report_id for e in db.ajoutes] == [2]


@pytest.mark.asyncio
async def test_l_administrateur_qui_clot_n_est_pas_notifie_de_son_propre_doublon():
    canonique = _rapport(id=2, user_id=99)
    doublon = _rapport(id=4, user_id=99, duplicate_of=2)
    db = _Db([doublon])

    avis = await tracking.close_with_duplicates(db, canonique, "resolved", actor_user_id=99)

    assert canonique.status == "resolved" and doublon.status == "resolved"
    assert avis == []


def test_la_veille_et_le_triage_passent_par_la_meme_cloture():
    """Deux chemins de clôture, une seule règle : sinon l'un des deux oublie
    les doublons, ce qui est exactement le défaut d'origine."""
    from pathlib import Path

    source = (Path(tracking.__file__)).read_text()
    corps = source[source.index("async def run_tick("):source.index("async def bug_report_watch_loop(")]
    assert "close_with_duplicates(" in corps

    route = (Path(tracking.__file__).parent.parent / "routers" / "bug_reports.py").read_text()
    debut = route.index("async def update_bug_report(")
    assert "close_with_duplicates(" in route[debut:route.index("\n@router", debut)]
