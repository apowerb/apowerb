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


class ApiKeyStoreConfig(DBConfig):
    """Configuration for the API key store."""

    table_name: str = "saved_api_keys"


class ApiKeyStore(BaseModel):
    """Class to manage saved API keys in a database."""

    api_key_config: ApiKeyStoreConfig = ApiKeyStoreConfig()
    db_host: str = api_key_config.db_host
    db_name: str = settings.db_name
    db_url: str = f"{api_key_config.db_host}:{api_key_config.db_port}/{db_name}"
    table_name: str = api_key_config.table_name
    db_user: str = api_key_config.db_user
    db_password: str = api_key_config.db_password
    db_type: str = api_key_config.db_type
    db_schema: str = api_key_config.db_schema
    engine: Any = None
    metadata: Any = None
    api_key_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        self.metadata = MetaData(schema=self.db_schema)
        self.api_key_table = Table(
            self.table_name,
            self.metadata,
            Column("api_key_id", Integer, primary_key=True, autoincrement=True),
            Column("key_name", String),
            Column("provider", String),
            Column("api_key_value", String),
            Column("model", String, nullable=True),
            Column("model_api_base", String, nullable=True),
            Column("owner_id", String),
            Column("organization_id", String),
            Column("created_at", String),
            Column("updated_at", String),
            Column("status", String),
        )

    def create_table(self):
        """Create the saved_api_keys table in the database."""
        if not inspect(self.engine).has_table(self.table_name):
            self.metadata.create_all(self.engine)
            logger.info(
                f"API key store table '{self.table_name}' created successfully."
            )
        else:
            logger.info(f"API key store table '{self.table_name}' already exists.")
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
            "model": "VARCHAR",
            "model_api_base": "VARCHAR",
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

    def delete_api_key(self, api_key_id: int) -> None:
        """Delete an API key by its numeric ID."""
        delete_q = self.api_key_table.delete().where(
            self.api_key_table.c.api_key_id == api_key_id
        )
        with self.engine.begin() as conn:
            conn.execute(delete_q)
