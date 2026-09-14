"""Un appel réussi ne dit rien de ce qui a cassé.

Vécu le 14/09/2026 : un signalement fait depuis l'écran de facturation,
sans aucun appel en échec, a été classé dans une autre zone. Le front
avait enregistré, juste avant l'ouverture du formulaire, le préchargement
d'une page voisine — réussi. Faute d'échec, le service retenait « le
dernier appel » comme appel fautif : sa route passait avant celle de
l'écran pour déduire la zone, et son statut 200 entrait dans l'empreinte.

Sans échec, il n'y a pas d'appel fautif : la zone vient de l'écran, et
l'empreinte ne dépend pas de ce que le navigateur a chargé en dernier.
"""

import pytest

from apowerb.bug_reports.areas import BugArea
from apowerb.bug_reports.fingerprint import compute_fingerprint
from apowerb.bug_reports.service import _failing_call
from apowerb.schema.bug_report_schema import BugReportCreate


class _Resultat:
    def scalar_one_or_none(self):
        return None


class _Session:
    def __init__(self):
        self.ajoutes = []

    async def execute(self, *_args, **_kwargs):
        return _Resultat()

    def add(self, objet):
        self.ajoutes.append(objet)

    async def flush(self):
        for objet in self.ajoutes:
            if getattr(objet, "id", None) is None:
                objet.id = 1

    async def commit(self):
        pass

    async def refresh(self, objet):
        objet.id = 1


def _appel(path, status, error=None):
    return {"method": "GET", "path": path, "status": status, "error": error}


def test_sans_echec_il_n_y_a_pas_d_appel_fautif():
    appels = [_appel("/api/agents", 200), _appel("/rag?_rsc=1", 200)]

    assert _failing_call(appels) == {}


def test_un_echec_reste_l_appel_fautif_meme_suivi_d_un_succes():
    appels = [_appel("/api/models/keys", 500), _appel("/api/agents", 200)]

    assert _failing_call(appels)["path"] == "/api/models/keys"


@pytest.mark.asyncio
async def test_la_zone_et_l_empreinte_viennent_de_l_ecran_quand_tout_a_reussi():
    from apowerb.bug_reports import service

    db = _Session()
    charge = BugReportCreate(
        what_i_did="Ouvrir la page d'outils",
        observed="La liste reste vide",
        context={"route": "/agents/42/tools"},
        api_calls=[_appel("/api/agents/42", 200), _appel("/rag", 200)],
    )

    resultat = await service.create_bug_report(
        db, user_id=1, reporter_email="t@example.org", payload=charge
    )

    rapport = db.ajoutes[0]
    assert rapport.area == BugArea.TOOLS.value
    assert resultat.fingerprint == compute_fingerprint(
        route="/agents/42/tools", status=None, error_signature="La liste reste vide"
    )
