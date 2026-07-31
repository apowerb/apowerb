from typing import Any
from apowerb.configs.th2logger import setup_logging
from pydantic import BaseModel
from apowerb.helpers.database_connection import DBConfig

from sqlalchemy import (
    create_engine,
    Table,
    Column,
    Integer,
    String,
    MetaData,
    inspect,
)
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)

settings = get_settings()


class HubStoreConfig(DBConfig):
    """Configuration for the hub store."""

    table_name: str = "hub_agents"


class HubStore(BaseModel):
    """Class to manage the hub agent store in a database."""

    hub_config: HubStoreConfig = HubStoreConfig()
    db_host: str = hub_config.db_host
    db_name: str = settings.db_name
    db_url: str = f"{hub_config.db_host}:{hub_config.db_port}/{db_name}"
    table_name: str = hub_config.table_name
    db_user: str = hub_config.db_user
    db_password: str = hub_config.db_password
    db_type: str = hub_config.db_type
    db_schema: str = hub_config.db_schema
    engine: Any = None
    metadata: Any = None
    hub_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        self.metadata = MetaData(schema=self.db_schema)
        self.hub_table = Table(
            self.table_name,
            self.metadata,
            Column("hub_id", Integer, primary_key=True, autoincrement=True),
            Column("hub_name", String, nullable=False),
            Column("hub_description", String),
            Column("hub_category", String),
            Column("hub_tags", String),
            # Snapshot of the original agent config (no API keys)
            Column("agent_name", String),
            Column("agent_model", String),
            Column("agent_description", String),
            Column("agent_instruction", String),
            Column("agent_tools", String),
            Column("agent_type", String),
            Column("sub_agents", String),
            Column("sub_agents_snapshot", String),
            Column("loop_max_iterations", String),
            Column("loop_exit_instruction", String),
            Column("agent_skills", String),
            Column("memory_enabled", String),
            Column("artifacts_enabled", String),
            Column("guardrails_config", String),
            # Metadata
            Column("publisher_id", String),
            Column("publisher_org", String),
            Column("source_agent_id", String),
            Column("clone_count", Integer, default=0),
            Column("published_at", String),
            Column("updated_at", String),
            Column("status", String),
        )

    def create_table(self):
        """Create the hub_agents table in the database."""
        if not inspect(self.engine).has_table(self.table_name):
            self.metadata.create_all(self.engine)
            logger.info(f"Hub store table '{self.table_name}' created successfully.")
        else:
            logger.info(f"Hub store table '{self.table_name}' already exists.")
        self.ensure_columns()

    def ensure_columns(self):
        """Add new columns to existing tables if they don't exist."""
        from sqlalchemy import text, inspect as sa_inspect

        inspector = sa_inspect(self.engine)
        existing = [
            c["name"]
            for c in inspector.get_columns(self.table_name, schema=self.db_schema)
        ]
        new_cols: dict[str, str] = {
            "sub_agents_snapshot": "VARCHAR",
            "loop_max_iterations": "VARCHAR",
            "loop_exit_instruction": "VARCHAR",
            "agent_skills": "VARCHAR",
        }
        with self.engine.begin() as conn:
            for col, typ in new_cols.items():
                if col not in existing:
                    schema_prefix = f'"{self.db_schema}".' if self.db_schema else ""
                    conn.execute(
                        text(
                            f'ALTER TABLE {schema_prefix}"{self.table_name}" ADD COLUMN "{col}" {typ}'
                        )
                    )

    def get_list(self, query: Any) -> Any:
        """Execute a query and return all rows."""
        with self.engine.connect() as conn:
            result = conn.execute(query)
            return result.fetchall()
