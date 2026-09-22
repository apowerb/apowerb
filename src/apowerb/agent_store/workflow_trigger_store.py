"""Store des triggers de workflow — état d'exécution automatique par workflow.

Le nœud ``trigger`` du graphe (``config.kind``) reste la source de vérité de
CE QUE l'utilisateur a choisi (webhook, schedule, ...) : cette table ne
duplique que ce qu'il faut pour l'exécuter sans reparser le graphe à chaque
appel public, et ce que le graphe ne doit jamais contenir — un secret.

Deux champs à propos du jeton webhook/formulaire :

* ``token_hash`` — SHA-256 du jeton, utilisé pour le retrouver et le vérifier
  (comparaison en temps constant). Jamais inversible : une fuite de cette
  colonne ne redonne pas le jeton.
* ``token_encrypted`` — le même jeton chiffré (Fernet, ``ENCRYPT_KEY``, comme
  les jetons OAuth d'intégration). Nécessaire parce que ``GET
  /api/workflows/{wid}/triggers`` doit pouvoir réafficher ``webhook_url`` à
  chaque appel (le contrat ne réserve l'affichage unique qu'au secret HMAC) :
  un hash seul ne le permettrait pas. Le déchiffrement n'a lieu que pour la
  réponse à une requête authentifiée du propriétaire.

Le secret HMAC suit la même logique mais n'est, lui, jamais redonné après sa
génération : seul ``hmac_secret_encrypted`` est conservé, déchiffré une seule
fois en interne pour vérifier une signature entrante.

Une ligne par workflow : T1 ne porte qu'un trigger — celui du nœud
``trigger`` de tête — par workflow, comme le contrat le décrit.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import Boolean, Column, MetaData, String, Table, inspect

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig
from apowerb.helpers.sync_engine import create_sync_engine

logger = setup_logging(__name__)
settings = get_settings()


class WorkflowTriggerStoreConfig(DBConfig):
    table_name: str = "workflow_triggers"


class WorkflowTriggerStore(BaseModel):
    """Accès à la table ``workflow_triggers``."""

    config: WorkflowTriggerStoreConfig = WorkflowTriggerStoreConfig()
    db_url: str = f"{config.db_host}:{config.db_port}/{settings.db_name}"
    table_name: str = config.table_name
    db_user: str = config.db_user
    db_password: str = config.db_password
    db_type: str = config.db_type
    db_schema: str = config.db_schema
    engine: Any = None
    metadata: Any = None
    trigger_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_sync_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        # ``or None`` : un DB_SCHEMA vide signifie « pas de schéma », comme
        # les autres stores (voir workflow_store.py).
        self.metadata = MetaData(schema=self.db_schema or None)
        self.trigger_table = Table(
            self.table_name,
            self.metadata,
            Column("workflow_id", String, primary_key=True),
            Column("owner_id", String, nullable=False, index=True),
            # Le kind du nœud trigger de tête ("manual" par défaut).
            Column("kind", String, nullable=False),
            # Config du nœud trigger telle que publiée, JSON — jamais de secret.
            Column("config", String, nullable=False),
            # Armé seulement si le workflow est publié ET que le kind est
            # branché (voir workflow_triggers.AUTOMATABLE_KINDS).
            Column("active", Boolean, nullable=False, default=False),
            Column("token_hash", String, index=True),
            Column("token_encrypted", String),
            Column("hmac_enabled", Boolean, nullable=False, default=False),
            Column("hmac_secret_encrypted", String),
            Column("next_run_at", String),
            Column("last_fired_at", String),
            # "running" | "success" | "error" | "cancelled" | "skipped_overlap"
            Column("last_status", String),
            Column("last_run_id", String),
            Column("created_at", String),
            Column("updated_at", String),
        )

    def create_table(self):
        """Crée la table si elle manque. ``create_all`` est idempotent."""
        existed = inspect(self.engine).has_table(self.table_name)
        self.metadata.create_all(self.engine)
        logger.info(
            "Workflow trigger store table '%s' %s.",
            self.table_name,
            "already exists" if existed else "created successfully",
        )
