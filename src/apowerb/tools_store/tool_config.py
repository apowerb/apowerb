from typing import Any
from apowerb.configs.th2logger import setup_logging
from pydantic import BaseModel
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
from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)

settings = get_settings()


def get_tools_config():
    ToolConfigStore()


class ToolConfigStoreConfig(DBConfig):
    """Configuration for the tool config store."""

    table_name: str = "tool_configs"  # Or from settings if available


class ToolConfigStore(BaseModel):
    """Class to manage the tool config store in a database."""

    tool_config: ToolConfigStoreConfig = ToolConfigStoreConfig()

    db_host: str = tool_config.db_host
    db_name: str = settings.db_name
    db_url: str = f"{tool_config.db_host}:{tool_config.db_port}/{db_name}"
    table_name: str = tool_config.table_name
    db_user: str = tool_config.db_user
    db_password: str = tool_config.db_password
    db_type: str = tool_config.db_type
    db_schema: str = tool_config.db_schema
    engine: Any = create_engine(
        f"{db_type}://{db_user}:{db_password}@{db_url}?sslmode={settings.db_sslmode}"
    )
    metadata: Any = MetaData()
    tool_config_table: Any = Table(
        table_name,
        metadata,
        Column("tool_config_id", Integer, primary_key=True),
        Column("tool_config_name", String),
        Column("tool_name", String),  # basic.tool_advanced
        Column("tool_config_params", String),  # JSON string
        Column("tool_category", String),
        Column("tool_config_type", String),
        Column("owner_id", String),
        Column("project_id", String),
        Column("organization_id", String),
        Column("created_at", String),
        Column("updated_at", String),
        Column("status", String),
        UniqueConstraint(
            "tool_config_name",
            "organization_id",
            "project_id",
            name="unique_tool_config_name_per_org_proj",
        ),
        schema=db_schema,
    )

    def create_table(self):
        """Create the tool config metadata table in the database."""
        if not inspect(self.engine).has_table(self.table_name):
            self.metadata.create_all(self.engine)
            logger.info(
                f"Tool config store table '{self.table_name}' created successfully."
            )
        else:
            logger.info(f"Tool config store table '{self.table_name}' already exists.")

    def get_list_tool_configs(self, tool_configs_query: Any) -> Any:
        """Get a list of all tool configs from the database."""
        with self.engine.connect() as conn:
            result = conn.execute(tool_configs_query)
            return result.fetchall()

    def delete_tool_config(self, tool_config_id: int, owner_id: str) -> dict:
        """Delete a tool config restricted to ``owner_id``.

        A row owned by another user is left untouched and a 404 is returned —
        we deliberately don't leak existence.
        """
        delete_query = self.tool_config_table.delete().where(
            (self.tool_config_table.c.tool_config_id == tool_config_id)
            & (self.tool_config_table.c.owner_id == owner_id)
        )
        with self.engine.begin() as conn:
            result = conn.execute(delete_query)
            if result.rowcount == 0:
                return {
                    "status": 404,
                    "message": f"Tool config {tool_config_id} not found",
                }
            return {"status": 200, "message": "Tool config deleted successfully"}
