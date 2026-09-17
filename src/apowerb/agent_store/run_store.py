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

from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    MetaData,
    inspect,
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
            # "running" | "success" | "error" | "cancelled"
            Column("status", String, nullable=False, index=True),
            Column("attempts", Integer, default=1),
            Column("error_message", String),
            Column("created_at", String),
            Column("finished_at", String),
            # Le run dont celui-ci est le rejeu. Un rejeu n'écrase jamais son
            # original : il le cite.
            Column("replay_of", String, index=True),
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
