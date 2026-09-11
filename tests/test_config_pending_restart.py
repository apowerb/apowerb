"""Le bandeau « en attente de redémarrage » ne doit apparaître que s'il est vrai.

La règle générale est bonne : une valeur posée après le démarrage du processus
n'agit pas, parce que 33 modules capturent `Settings` à l'import. Mais depuis
que le ticketing lit sa configuration au moment de l'usage, ses trois variables
font exception — et l'écran continuait de réclamer un redémarrage inutile.

Ce test porte sur ce que l'écran lit réellement, `VariableState.pending_restart`,
pas seulement sur le drapeau du catalogue : le second sans le premier laisserait
passer un catalogue correct branché sur un calcul inchangé.
"""

from datetime import timedelta

from apowerb.core.config_admin import store
from apowerb.core.config_admin.catalog import BY_NAME


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    """Rend les lignes de la table, quelle que soit la requête."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_args, **_kwargs):
        return _Rows(self._rows)


async def _etat(nom, *, posee_apres_demarrage=True):
    quand = store.PROCESS_STARTED_AT + timedelta(
        seconds=60 if posee_apres_demarrage else -60
    )
    db = _Db([(nom, quand, "admin@example.org")])
    etats = await store.list_states(db)
    return next(e for e in etats if e.name == nom)


async def test_une_variable_du_ticketing_ne_reclame_pas_de_redemarrage(monkeypatch):
    monkeypatch.setattr(store, "env_holds", lambda name, env=None: False)
    monkeypatch.setattr(store, "overlay_applied", lambda name, env=None: False)

    etat = await _etat("BUG_REPORT_GITHUB_PROJECT")

    assert etat.source == store.SOURCE_DATABASE
    assert etat.pending_restart is False


async def test_une_autre_variable_reclame_toujours_un_redemarrage(monkeypatch):
    monkeypatch.setattr(store, "env_holds", lambda name, env=None: False)
    monkeypatch.setattr(store, "overlay_applied", lambda name, env=None: False)

    etat = await _etat("DEFAULT_LLM_MODEL")

    assert etat.source == store.SOURCE_DATABASE
    assert etat.pending_restart is True


async def test_le_catalogue_et_le_calcul_disent_la_meme_chose(monkeypatch):
    """Garde contre une dérive : tout ce qui est déclaré appliqué à chaud doit
    l'être aussi dans l'état que lit l'écran."""
    monkeypatch.setattr(store, "env_holds", lambda name, env=None: False)
    monkeypatch.setattr(store, "overlay_applied", lambda name, env=None: False)

    for nom, variable in BY_NAME.items():
        if variable.applied_live:
            assert (await _etat(nom)).pending_restart is False, nom
