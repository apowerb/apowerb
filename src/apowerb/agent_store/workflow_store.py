"""Store des workflows — le graphe du canvas devient un objet persisté.

Jusqu'ici un « workflow » était un agent composite dont ``sub_agents`` tenait
la liste du canvas (roadmap#54) : impossible d'y ranger une branche, un outil
ou un routeur. Un workflow a désormais sa table, son graphe JSON versionné
(``apowerb.core.workflow_graph.WorkflowGraph``) et un historique restaurable
calqué sur ``agent_revisions``.
"""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import Column, Integer, MetaData, String, Table, inspect

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig
from apowerb.helpers.sync_engine import create_sync_engine

logger = setup_logging(__name__)
settings = get_settings()


class WorkflowStoreConfig(DBConfig):
    table_name: str = "workflows"


class WorkflowStore(BaseModel):
    """Accès aux tables ``workflows`` et ``workflow_revisions``."""

    config: WorkflowStoreConfig = WorkflowStoreConfig()
    db_url: str = f"{config.db_host}:{config.db_port}/{settings.db_name}"
    table_name: str = config.table_name
    db_user: str = config.db_user
    db_password: str = config.db_password
    db_type: str = config.db_type
    db_schema: str = config.db_schema
    engine: Any = None
    metadata: Any = None
    workflow_table: Any = None
    revision_table: Any = None
    suggest_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_sync_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        # ``or None`` : un DB_SCHEMA vide signifie « pas de schéma ».
        self.metadata = MetaData(schema=self.db_schema or None)
        self.workflow_table = Table(
            self.table_name,
            self.metadata,
            Column("workflow_id", String, primary_key=True),
            Column("name", String, nullable=False),
            Column("description", String),
            Column("owner_id", String, nullable=False, index=True),
            Column("organization_id", String),
            # WorkflowGraph sérialisé ; un brouillon peut être sémantiquement
            # incomplet, seule sa structure est garantie.
            Column("graph", String, nullable=False),
            # "draft" | "published"
            Column("status", String, nullable=False),
            # Verrou optimiste : une écriture qui cite une version dépassée
            # est refusée au lieu d'écraser l'autre onglet.
            Column("version", Integer, nullable=False),
            Column("created_at", String),
            Column("updated_at", String),
        )
        self.revision_table = Table(
            "workflow_revisions",
            self.metadata,
            Column("revision_id", Integer, primary_key=True, autoincrement=True),
            Column("workflow_id", String, nullable=False, index=True),
            Column("version", Integer, nullable=False),
            Column("name", String),
            Column("description", String),
            Column("graph", String, nullable=False),
            Column("status", String),
            Column("owner_id", String, nullable=False, index=True),
            Column("saved_at", String),
            # "update" | "restore"
            Column("reason", String),
        )
        # Adoption des pastilles « Étape suivante » (roadmap#88) : des totaux
        # par jour × source × type, rien qui désigne un utilisateur ou un graphe.
        # ``node_type = ""`` porte les totaux de la source.
        self.suggest_table = Table(
            "workflow_suggest_daily",
            self.metadata,
            Column("day", String, primary_key=True),
            Column("source", String, primary_key=True),
            Column("node_type", String, primary_key=True),
            Column("steps", Integer, nullable=False),
            Column("shown", Integer, nullable=False),
            Column("accepted", Integer, nullable=False),
        )

    def create_table(self):
        """Crée les tables manquantes. ``create_all`` est idempotent."""
        existed = inspect(self.engine).has_table(self.table_name)
        self.metadata.create_all(self.engine)
        logger.info(
            "Workflow store table '%s' %s.",
            self.table_name,
            "already exists" if existed else "created successfully",
        )
