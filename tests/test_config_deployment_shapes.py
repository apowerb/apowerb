"""Ce que l'écran dit d'une variable sur un déploiement réel.

Deux formes mesurées le 09/09/2026 sur le cluster Helm, reproduites ici parce
que c'est ce que l'administrateur LIT avant d'écrire, et que se tromper là lui
fait croire qu'il a configuré quelque chose d'inerte.
"""

from __future__ import annotations

import pytest

from apowerb.core.config_admin import overlay, store


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    """Une session dont `execute` s'attend et dont le Result est synchrone."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    async def execute(self, *_args, **_kwargs):
        return _Result(self._rows)


# Relevé sur le cluster : trois variables portent une valeur en clair dans le
# manifeste, quatre viennent d'un `secretKeyRef` en `optional: true` — donc
# ABSENTES de l'environnement tant que la clé manque au Secret. Aucune n'est
# posée vide.
POSEES_EN_CLAIR = ("TH2ETL_BASE_URL", "OTEL_EXPORTER_OTLP_ENDPOINT", "STORAGE_MODE")
SECRET_KEY_REF_ABSENTES = (
    "TH2ETL_API_KEY",
    "DEFAULT_LLM_API_KEY",
    "GOOGLE_INTEGRATION_CLIENT_SECRET",
    "MICROSOFT_INTEGRATION_CLIENT_SECRET",
)


@pytest.fixture
def cluster(monkeypatch):
    """L'environnement d'un pod du cluster, tel que mesuré."""
    for name in POSEES_EN_CLAIR:
        monkeypatch.setenv(name, "une-valeur-du-deploiement")
    for name in SECRET_KEY_REF_ABSENTES:
        # `optional: true` + clé absente du Secret = variable absente, pas vide.
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(store.OVERLAY_MARKER, raising=False)


@pytest.mark.asyncio
async def test_une_variable_du_manifeste_est_annoncee_comme_imposee(cluster):
    etats = {e.name: e for e in await store.list_states(_Db())}
    for name in POSEES_EN_CLAIR:
        assert etats[name].source == store.SOURCE_ENV, name


@pytest.mark.asyncio
async def test_un_secretkeyref_optionnel_absent_reste_posable(cluster):
    """`optional: true` et clé manquante : l'écran DOIT offrir le champ, sinon
    le seul chemin pour configurer l'installation serait de repasser par le
    Secret — c'est-à-dire par le code."""
    etats = {e.name: e for e in await store.list_states(_Db())}
    for name in SECRET_KEY_REF_ABSENTES:
        assert etats[name].source == store.SOURCE_UNSET, name


@pytest.mark.asyncio
async def test_l_overlay_ne_recouvre_que_les_variables_absentes(cluster):
    """La règle de précédence sur la forme réelle : on n'exporte que les
    quatre absentes, jamais les trois que le manifeste impose."""
    posees = {name: "peu-importe" for name in POSEES_EN_CLAIR + SECRET_KEY_REF_ABSENTES}
    noms = [ligne.split("=", 1)[0] for ligne in overlay.overlay_lines(posees)]
    assert set(noms) == set(SECRET_KEY_REF_ABSENTES) | {store.OVERLAY_MARKER}


# --------------------------------------------------------------------------
# Le cycle complet : poser, redémarrer, reposer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_une_valeur_posee_reste_modifiable_apres_redemarrage(monkeypatch):
    """Le défaut que cette forme révèle.

    Au redémarrage, l'entrypoint exporte la valeur posée : elle est DANS
    l'environnement du processus qui sert l'écran. Une lecture naïve de
    `os.environ` la déclarerait alors « imposée par le déploiement » — et
    l'écran retirerait le champ de la variable qu'on venait d'y poser, sans
    plus aucun moyen de la corriger depuis l'interface.

    D'où le marqueur : l'overlay dit quels NOMS il a lui-même posés (des noms,
    jamais des valeurs), et le processus sait distinguer sa propre empreinte
    de celle du déploiement.
    """
    monkeypatch.setenv("SMTP_HOST", "valeur-exportee-par-l-entrypoint")
    monkeypatch.setenv(store.OVERLAY_MARKER, "SMTP_HOST")

    db = _Db([("SMTP_HOST", None, "chef@example.com")])
    etat = {e.name: e for e in await store.list_states(db)}["SMTP_HOST"]

    assert etat.source == store.SOURCE_DATABASE
    assert etat.updated_by == "chef@example.com"


@pytest.mark.asyncio
async def test_le_deploiement_reprend_la_main_meme_sur_une_variable_posee(monkeypatch):
    """L'inverse, et c'est le chemin de sortie de l'opérateur : dès qu'il pose
    la variable dans son déploiement, `export-env` ne l'exporte plus, donc elle
    n'est plus dans le marqueur, donc l'écran la rend au déploiement — même si
    la ligne en base existe toujours."""
    monkeypatch.setenv("SMTP_HOST", "valeur-du-deploiement")
    monkeypatch.setenv(store.OVERLAY_MARKER, "SMTP_PORT")  # SMTP_HOST n'y est plus

    db = _Db([("SMTP_HOST", None, "chef@example.com")])
    etat = {e.name: e for e in await store.list_states(db)}["SMTP_HOST"]

    assert etat.source == store.SOURCE_ENV


def test_le_marqueur_ne_transporte_que_des_noms():
    lignes = overlay.overlay_lines({"SMTP_PASSWORD": "secret-factice"}, env={})
    marqueur = [l for l in lignes if l.startswith(store.OVERLAY_MARKER)]
    assert marqueur == [f"{store.OVERLAY_MARKER}='SMTP_PASSWORD'"]


def test_pas_de_marqueur_quand_il_n_y_a_rien_a_exporter():
    """Un marqueur vide exporté par-dessus un marqueur hérité effacerait
    l'information ; ne rien écrire du tout est plus simple et plus sûr."""
    assert overlay.overlay_lines({}, env={}) == []
    assert overlay.overlay_lines({"SMTP_HOST": "x"}, env={"SMTP_HOST": "y"}) == []
