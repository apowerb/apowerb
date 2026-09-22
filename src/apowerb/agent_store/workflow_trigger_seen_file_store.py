"""Store de déduplication du trigger ``file`` (T2) — un fichier déjà vu.

``core.workflow_file_triggers.poll_file_trigger`` compare, à chaque sondage,
la liste courante des fichiers du dossier surveillé à ce qui a été vu au
sondage précédent. Cette table est cette mémoire : sans elle, chaque
redémarrage du processus (ou chaque réplica) redéclencherait un run pour
TOUS les fichiers déjà présents, pas seulement les nouveaux.

Clé primaire composite ``(workflow_id, file_id)`` : un fichier est vu par UN
trigger précis (un workflow peut surveiller un dossier, un autre workflow
peut surveiller le même dossier indépendamment — chacun sa propre mémoire).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import Column, MetaData, String, Table, inspect

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig
from apowerb.helpers.sync_engine import create_sync_engine

logger = setup_logging(__name__)
settings = get_settings()


class WorkflowTriggerSeenFileStoreConfig(DBConfig):
    table_name: str = "workflow_trigger_seen_files"


class WorkflowTriggerSeenFileStore(BaseModel):
    """Accès à la table ``workflow_trigger_seen_files``."""

    config: WorkflowTriggerSeenFileStoreConfig = WorkflowTriggerSeenFileStoreConfig()
    db_url: str = f"{config.db_host}:{config.db_port}/{settings.db_name}"
    table_name: str = config.table_name
    db_user: str = config.db_user
    db_password: str = config.db_password
    db_type: str = config.db_type
    db_schema: str = config.db_schema
    engine: Any = None
    metadata: Any = None
    seen_file_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_sync_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        self.metadata = MetaData(schema=self.db_schema or None)
        self.seen_file_table = Table(
            self.table_name,
            self.metadata,
            Column("workflow_id", String, primary_key=True),
            Column("file_id", String, primary_key=True),
            Column("modified_at", String),
            Column("seen_at", String),
        )

    def create_table(self):
        existed = inspect(self.engine).has_table(self.table_name)
        self.metadata.create_all(self.engine)
        logger.info(
            "Workflow trigger seen-file store table '%s' %s.",
            self.table_name,
            "already exists" if existed else "created successfully",
        )
