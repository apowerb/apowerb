"""Store des exécutions d'agents — une ligne par run, quel qu'en soit le déclencheur.

La reprise après échec n'existait que sur le chemin webhook, et seulement pour
Outlook : ``webhook_logs`` porte la charge utile, le compteur de tentatives et
les pièces jointes conservées, donc un message peut être retraité. Un run lancé
ailleurs — canvas de workflow, ordonnanceur, appel API — ne laissait rien
derrière lui ; le routeur des workflows tenait son état dans un dictionnaire de
processus, vidé à la fin du flux.

Cette table est l'entrée rejouable qui manquait. Elle ne remplace pas
``webhook_logs`` : celui-ci reste la file du worker de backlog, avec sa propre
idempotence. Les deux convergeront le jour où le moteur d'exécution sera unifié.
"""

import json
from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    Table,
    Column,
    Index,
    bindparam,
    Integer,
    String,
    MetaData,
    inspect,
    text,
)

from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig
from apowerb.helpers.sync_engine import create_sync_engine
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)

settings = get_settings()


class RunStoreConfig(DBConfig):
    """Configuration du store des runs."""

    table_name: str = "agent_runs"


class RunStore(BaseModel):
    """Accès à la table ``agent_runs``."""

    run_config: RunStoreConfig = RunStoreConfig()
    db_host: str = run_config.db_host
    db_name: str = settings.db_name
    db_url: str = f"{run_config.db_host}:{run_config.db_port}/{db_name}"
    table_name: str = run_config.table_name
    db_user: str = run_config.db_user
    db_password: str = run_config.db_password
    db_type: str = run_config.db_type
    db_schema: str = run_config.db_schema
    engine: Any = None
    metadata: Any = None
    run_table: Any = None
    trace_table: Any = None
    failure_alert_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_sync_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        # ``or None`` : un DB_SCHEMA vide signifie « pas de schéma », pas un
        # schéma nommé "". Sans cette normalisation, ``create_all`` cherche la
        # table dans un schéma introuvable et échoue sur un CREATE en doublon.
        self.metadata = MetaData(schema=self.db_schema or None)
        self.run_table = Table(
            self.table_name,
            self.metadata,
            # L'identifiant vient de l'appelant (le ``wid`` du front) quand il
            # en fournit un : c'est ce qui permet de recoller un flux SSE déjà
            # ouvert à sa ligne en base.
            Column("run_id", String, primary_key=True),
            # "workflow" | "schedule" | "chat" | "webhook" — d'où vient le run.
            Column("trigger", String, nullable=False),
            Column("owner_id", String, nullable=False, index=True),
            Column("organization_id", String),
            Column("agent_ids", String),
            # L'entrée conservée, sans laquelle il n'y a pas de rejeu possible.
            Column("config", String),
            Column("input_file_name", String),
            Column("input_file_path", String),
            # "running" | "success" | "error" | "cancelled" | "awaiting"
            Column("status", String, nullable=False, index=True),
            Column("attempts", Integer, default=1),
            Column("error_message", String),
            Column("created_at", String),
            Column("finished_at", String),
            # Le run dont celui-ci est le rejeu. Un rejeu n'écrase jamais son
            # original : il le cite.
            Column("replay_of", String, index=True),
            # Les outils qu'un run d'agent a appelés, en JSON. ``NULL`` veut
            # dire « inconnu », pas « aucun » : c'est ce qui décide si un
            # échec peut être rejoué sans refaire un effet de bord.
            Column("tools_executed", String),
            # Run arrêté sur une décision humaine (statut "awaiting") : son
            # instantané en JSON (graphe, payload, sorties, routes, question),
            # relu par la reprise, et la personne à qui la décision revient
            # en plus du propriétaire.
            Column("paused_state", String),
            Column("assignee", String, index=True),
            # La question en attente seule (petite, lue par les listes sans
            # charger l'instantané) et l'heure de la reprise, qui remplace
            # ``created_at`` pour juger un run ``running`` orphelin.
            Column("decision", String),
            Column("resumed_at", String),
            # Échéance de la décision en attente : passé ce moment, le run est
            # clos en ``error``. ISO UTC ; les pauses plus anciennes gardent
            # le format de ``created_at`` (heure locale du serveur).
            Column("expires_at", String),
            # Avis de la décision (``core.decision_notices``) : heure du
            # rappel (ISO UTC), puis ce qui est déjà parti — chaque envoi est
            # réservé sur sa colonne avant de partir, une seule fois.
            # ``end_notice`` vaut ``pending`` quand une fin sans réponse
            # (expirée, abandonnée, illisible) reste à annoncer.
            Column("remind_at", String),
            Column("notified_at", String),
            Column("reminded_at", String),
            Column("end_notice", String),
            # Le workflow d'un run de graphe (``config.workflow_id``), en
            # colonne pour que l'historique d'un workflow se filtre en SQL ;
            # indexé avec ``created_at`` (voir ``Index`` plus bas).
            Column("workflow_id", String),
            # L'échec sous forme structurée, en JSON : ``code`` (traduit par
            # l'interface), ``params`` et ``ref``. Le nœud fautif est dans
            # ``failure_node``. ``error_message`` garde le texte, pour les
            # lecteurs existants.
            Column("error_info", String),
            # Alerte d'échec (``workflow_alerts``) : réservée avant l'envoi,
            # puis son issue (sent, grouped, off, failed). Une seule par run.
            Column("failure_alert", String),
            # Run de graphe en ``error`` : l'instantané scellé de ce qui était
            # fait (sorties, routes), relu par « Reprendre ici », et le nœud
            # en échec, qui reste lisible une fois l'instantané purgé.
            Column("failure_state", String),
            Column("failure_node", String),
            # L'historique d'un workflow : filtré par workflow, trié par date.
            Index(f"ix_{self.table_name}_workflow_created", "workflow_id", "created_at"),
        )
        # Le déroulé d'un run (ses événements SSE, plafonnés) : à part, pour
        # que les listes de runs ne le chargent jamais.
        self.trace_table = Table(
            f"{self.table_name}_traces",
            self.metadata,
            Column("run_id", String, primary_key=True),
            Column("events", String),
            Column("truncated", Integer, default=0),
            Column("updated_at", String),
        )
        # Dernière alerte d'échec par workflow et échecs regroupés depuis :
        # au plus une alerte par heure pour un workflow qui échoue en boucle.
        self.failure_alert_table = Table(
            "workflow_failure_alerts",
            self.metadata,
            Column("workflow_id", String, primary_key=True),
            Column("owner_id", String, nullable=False),
            Column("last_sent_at", String, nullable=False),
            Column("grouped", Integer, nullable=False, default=0),
        )

    def create_table(self):
        """Crée la table si elle manque.

        ``create_all`` est appelé inconditionnellement — il est idempotent.
        Le conditionner à l'absence de la table, c'est se garantir qu'une
        table ajoutée plus tard n'apparaîtra jamais sur une base déjà en
        service : invisible en test, visible en production seulement.
        """
        existed = inspect(self.engine).has_table(self.table_name)
        self.metadata.create_all(self.engine)
        logger.info(
            "Run store table '%s' %s.",
            self.table_name,
            "already exists" if existed else "created successfully",
        )
        self.ensure_columns()

    def ensure_columns(self):
        """Ajoute aux tables déjà en service les colonnes venues après elles.

        ``create_all`` ne modifie jamais une table existante : sans ce
        rattrapage, une base créée avant la colonne ne l'aurait jamais.
        """
        existing = {
            c["name"]
            for c in inspect(self.engine).get_columns(
                self.table_name, schema=self.db_schema or None
            )
        }
        added_later: dict[str, str] = {
            "tools_executed": "VARCHAR",
            "paused_state": "VARCHAR",
            "assignee": "VARCHAR",
            "decision": "VARCHAR",
            "resumed_at": "VARCHAR",
            "expires_at": "VARCHAR",
            "remind_at": "VARCHAR",
            "notified_at": "VARCHAR",
            "reminded_at": "VARCHAR",
            "end_notice": "VARCHAR",
            "workflow_id": "VARCHAR",
            "error_info": "VARCHAR",
            "failure_alert": "VARCHAR",
            "failure_state": "VARCHAR",
            "failure_node": "VARCHAR",
        }
        schema_prefix = f'"{self.db_schema}".' if self.db_schema else ""
        # Deux réplicas qui démarrent ensemble : sur Postgres, le second
        # ALTER ne doit pas échouer sur la colonne que le premier vient
        # d'ajouter.
        if_not_exists = (
            "IF NOT EXISTS " if self.engine.dialect.name == "postgresql" else ""
        )
        with self.engine.begin() as conn:
            for col, typ in added_later.items():
                if col not in existing:
                    conn.execute(
                        text(
                            f'ALTER TABLE {schema_prefix}"{self.table_name}" '
                            f'ADD COLUMN {if_not_exists}"{col}" {typ}'
                        )
                    )
                    logger.info("Run store: colonne %s ajoutée.", col)
            if if_not_exists:
                # ``index=True`` ne vaut que pour une table neuve.
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "ix_{self.table_name}_assignee" '
                        f'ON {schema_prefix}"{self.table_name}" ("assignee")'
                    )
                )
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "ix_{self.table_name}_workflow_created" '
                        f'ON {schema_prefix}"{self.table_name}" ("workflow_id", "created_at")'
                    )
                )
        # Après le COMMIT des ALTER : le rattrapage parcourt toute la table et
        # ne doit pas la garder verrouillée. Un échec n'empêche pas le
        # démarrage ; seuls les runs anciens manquent alors à l'historique.
        if "workflow_id" not in existing and "config" in existing:
            try:
                self._backfill_workflow_id()
            except Exception:  # noqa: BLE001
                logger.exception("Run store: rattachement des runs à leur workflow échoué.")

    def _backfill_workflow_id(self) -> int:
        """Rattache à leur workflow les runs de graphe consignés avant la colonne.

        Une seule fois, quand la colonne vient d'être ajoutée : sans ce
        rattrapage, l'historique d'un workflow commencerait au déploiement.
        Une seule requête ``UPDATE`` quand la base sait lire le JSON. Avant
        Postgres 16 (pas de ``IS JSON``), une ``config`` qui n'en est pas fait
        échouer la requête : le rattrapage repasse alors par lots.
        """
        schema_prefix = f'"{self.db_schema}".' if self.db_schema else ""
        table = f'{schema_prefix}"{self.table_name}"'
        dialect = self.engine.dialect.name
        if dialect == "postgresql":
            version = self.engine.dialect.server_version_info or (0,)
            if not version or version[0] == 0:
                with self.engine.connect() as conn:
                    version = conn.dialect.server_version_info or (0,)
            extract = "config::json->>'workflow_id'"
            guard = "config IS JSON OBJECT" if version[0] >= 16 else "TRUE"
        elif dialect == "sqlite":
            extract, guard = "json_extract(config, '$.workflow_id')", "json_valid(config)"
        else:
            extract = None
        if extract is not None:
            try:
                with self.engine.begin() as conn:
                    updated = conn.execute(
                        text(
                            f"UPDATE {table} SET workflow_id = {extract} "
                            f"WHERE workflow_id IS NULL AND config LIKE '%\"workflow_id\"%' "
                            f"AND {guard} AND {extract} IS NOT NULL"
                        )
                    ).rowcount
                logger.info("Run store: %d run(s) rattaché(s) à leur workflow.", updated)
                return updated
            except Exception as exc:  # noqa: BLE001 - repli par lots
                logger.warning(
                    "Run store: rattachement en une requête impossible (%s), repli par lots.",
                    type(exc).__name__,
                )
        return self._backfill_workflow_id_by_row()

    def _backfill_workflow_id_by_row(self, batch: int = 1_000) -> int:
        """Repli : lu en Python, écrit par lots, chacun dans sa transaction."""
        t = self.run_table
        with self.engine.connect() as conn:
            rows = conn.execute(
                t.select()
                .with_only_columns(t.c.run_id, t.c.config)
                .where(t.c.workflow_id.is_(None), t.c.config.like('%"workflow_id"%'))
            ).fetchall()
        found = []
        for run_id, config in rows:
            try:
                workflow_id = (json.loads(config or "{}") or {}).get("workflow_id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if workflow_id:
                found.append({"rid": run_id, "wid": str(workflow_id)})
        update = (
            t.update()
            .where(t.c.run_id == bindparam("rid"))
            .values(workflow_id=bindparam("wid"))
        )
        for start in range(0, len(found), batch):
            with self.engine.begin() as conn:
                conn.execute(update, found[start : start + batch])
        logger.info("Run store: %d run(s) rattaché(s) à leur workflow.", len(found))
        return len(found)
