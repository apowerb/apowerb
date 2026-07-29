from typing import Any
from th2agent.configs.th2logger import setup_logging

from pydantic import BaseModel
from th2agent.helpers.database_connection import DBConfig

from sqlalchemy import (
    create_engine,
    Table,
    Column,
    Integer,
    String,
    MetaData,
    inspect,
    UniqueConstraint,
)
from th2agent.configs.settings import get_settings

logger = setup_logging(__name__)

settings = get_settings()


class AgentStoreConfig(DBConfig):
    """Configuration for the agent store."""

    table_name: str = settings.db_agent_store_table_name


class AgentStore(BaseModel):
    """Class to manage the agent store in a database.

    Attributes:
        db_host: str - The database host.
        db_name: str - The database name.
        db_url: str - The database URL.
        table_name: str - The name of the agent table.
        db_user: str - The database user.
        db_password: str - The database password.
        db_type: str - The type of the database.
        db_schema: str - The database schema.
        engine: Any - The SQLAlchemy engine instance.
        metadata: Any - The SQLAlchemy metadata instance.
        agent_table: Any - The SQLAlchemy table instance for agents.
    """

    agent_config: AgentStoreConfig = AgentStoreConfig()
    db_host: str = agent_config.db_host
    db_name: str = settings.db_name
    db_url: str = f"{agent_config.db_host}:{agent_config.db_port}/{db_name}"
    table_name: str = agent_config.table_name
    db_user: str = agent_config.db_user
    db_password: str = agent_config.db_password
    db_type: str = agent_config.db_type
    db_schema: str = agent_config.db_schema
    engine: Any = None
    metadata: Any = None
    agent_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        self.metadata = MetaData(schema=self.db_schema)
        self.agent_table = Table(
            self.table_name,
            self.metadata,
            Column("agent_id", Integer, primary_key=True, autoincrement=True),
            Column("agent_name", String, nullable=False),
            Column("agent_model", String, nullable=False),
            Column("agent_model_params", String),
            Column("agent_description", String),
            Column("agent_instruction", String),
            Column("agent_tools", String),
            Column("agent_type", String),
            Column("sub_agents", String),
            Column("output_key", String),
            Column("output_schema_name", String),
            Column("skip_when_upstream", String),
            Column("input_schema", String),
            Column("output_schema", String),
            Column("code_executor", String),
            Column("created_at", String),
            Column("updated_at", String),
            Column("status", String),
            Column("organization_id", String, nullable=False),
            Column("project_id", String, nullable=False),
            Column("owner_id", String),
            Column("tags", String),
            Column("guardrails_config", String),
            Column("memory_enabled", String),
            Column("artifacts_enabled", String),
            Column("superagent_template_id", String),
            Column("superagent_template_version_hash", String),
            Column("loop_max_iterations", String),
            Column("loop_exit_instruction", String),
            Column("mcp_servers", String),
            Column("agent_skills", String),
            Column("hub_origin_id", String),
            UniqueConstraint(
                "agent_name",
                "organization_id",
                "project_id",
                name="unique_agent_name_per_org_proj",
            ),
        )

    def create_table(self):
        """Create the agent metadata table in the database."""
        if not inspect(self.engine).has_table(self.table_name):
            self.metadata.create_all(self.engine)
            logger.info(f"Agent store table '{self.table_name}' created successfully.")
        else:
            logger.info(f"Agent store table '{self.table_name}' already exists.")
        self.ensure_columns()

    def ensure_columns(self):
        """Add new columns to existing tables if they don't exist."""
        from sqlalchemy import text, inspect as sa_inspect

        inspector = sa_inspect(self.engine)
        existing = [
            c["name"]
            for c in inspector.get_columns(self.table_name, schema=self.db_schema)
        ]
        new_cols = {
            "output_key": "VARCHAR",
            "output_schema_name": "VARCHAR",
            "skip_when_upstream": "VARCHAR",
            "guardrails_config": "VARCHAR",
            "memory_enabled": "VARCHAR",
            "artifacts_enabled": "VARCHAR",
            "superagent_template_id": "VARCHAR",
            "loop_max_iterations": "VARCHAR",
            "loop_exit_instruction": "VARCHAR",
            "mcp_servers": "VARCHAR",
            "agent_skills": "VARCHAR",
            "hub_origin_id": "VARCHAR",
            # Snapshot of the source template's hash-relevant fields at
            # creation time. The UI compares it to the live template hash
            # to surface "template updated, click to sync" banners.
            "superagent_template_version_hash": "VARCHAR",
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

    def get_list_agents(self, agents_query: Any) -> Any:
        """Get a list of all agents from the database."""
        with self.engine.connect() as conn:
            result = conn.execute(agents_query)
            return result.fetchall()
