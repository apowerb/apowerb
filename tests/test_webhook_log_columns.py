"""La table webhook_logs doit porter toutes les colonnes que le modele lit.

Le 11/09/2026, l'onglet Activity d'agent-dev rendait 500 sur CHAQUE requete :
`column webhook_logs.email_body_html does not exist`. La base avait 20
colonnes, le modele en interrogeait 23.

La cause n'est pas une colonne oubliee dans un script, c'est l'absence de
mecanisme : `ensure_webhook_logs_table` ne cree la table que si elle est
absente, et ne touche a rien quand elle existe. Toute colonne ajoutee au
modele apres la creation initiale manquait donc pour toujours -- en silence,
l'exception du demarrage etant journalisee puis avalee.

Le dernier test ci-dessous est le garde qui manquait : il compare le modele
au CREATE TABLE et exige que l'ecart soit declare dans _COLONNES_AJOUTEES.
Il rougit a la prochaine colonne ajoutee sans migration, avant que la panne
n'atteigne un environnement.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import apowerb.helpers.webhook_log_migration as mod
from apowerb.models import WebhookLog


def _faux_moteur(colonnes_presentes, table_presente=True):
    """Un moteur qui declare les colonnes demandees, et enregistre les ALTER."""
    conn = MagicMock()
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    inspector = MagicMock()
    inspector.get_table_names.return_value = ["webhook_logs"] if table_presente else []
    inspector.get_columns.return_value = [{"name": n} for n in colonnes_presentes]
    return engine, conn, inspector


def _sql_executes(conn):
    return [str(appel.args[0]) for appel in conn.execute.call_args_list]


def test_ajoute_les_colonnes_manquantes():
    toutes = [n for n, _ in mod._COLONNES_AJOUTEES]
    engine, conn, inspector = _faux_moteur(["id", "user_id"])  # aucune des gerees
    with patch.object(mod, "create_engine", return_value=engine), \
         patch.object(mod, "inspect", return_value=inspector):
        mod.ensure_webhook_logs_columns()

    sql = " ".join(_sql_executes(conn))
    for nom in toutes:
        assert f"ADD COLUMN IF NOT EXISTS {nom}" in sql, f"{nom} n'a pas ete ajoutee"
    conn.commit.assert_called_once()


def test_ne_touche_a_rien_quand_tout_est_la():
    presentes = ["id", "user_id"] + [n for n, _ in mod._COLONNES_AJOUTEES]
    engine, conn, inspector = _faux_moteur(presentes)
    with patch.object(mod, "create_engine", return_value=engine), \
         patch.object(mod, "inspect", return_value=inspector):
        mod.ensure_webhook_logs_columns()

    assert _sql_executes(conn) == []
    conn.commit.assert_not_called()


def test_n_altere_pas_une_table_absente():
    # ensure_webhook_logs_table la creera avec ses colonnes d'origine.
    engine, conn, inspector = _faux_moteur([], table_presente=False)
    with patch.object(mod, "create_engine", return_value=engine), \
         patch.object(mod, "inspect", return_value=inspector):
        mod.ensure_webhook_logs_columns()

    assert _sql_executes(conn) == []


def test_toute_colonne_du_modele_est_creee_ou_declaree():
    """Le garde : modele == CREATE TABLE + _COLONNES_AJOUTEES.

    Sans lui, ajouter une colonne au modele casse silencieusement toute base
    existante -- ce qui est exactement arrive a email_body_html.
    """
    source = Path(mod.__file__).read_text(encoding="utf-8")
    creation = source.split("CREATE TABLE IF NOT EXISTS", 1)[1].split('""")', 1)[0]
    # Une ligne de definition commence par un nom de colonne ; on ignore la
    # premiere ligne (le nom de la table) et les lignes de contrainte.
    dans_create = set()
    for ligne in creation.splitlines()[1:]:
        m = re.match(r"\s{20,}([a-z_]+)\s+[A-Z]", ligne)
        if m:
            dans_create.add(m.group(1))
    assert "email_subject" in dans_create, "le parsing du CREATE TABLE a echoue"

    declarees = {n for n, _ in mod._COLONNES_AJOUTEES}
    attendues = {c.name for c in WebhookLog.__table__.columns}
    manquantes = attendues - dans_create - declarees
    assert not manquantes, (
        "Ces colonnes du modele WebhookLog ne sont ni dans le CREATE TABLE ni "
        f"dans _COLONNES_AJOUTEES : {sorted(manquantes)}. Toute base existante "
        "rendra 500 sur /api/webhooks/logs. Ajoute-les a _COLONNES_AJOUTEES "
        "avec leur type SQL et NULL."
    )
