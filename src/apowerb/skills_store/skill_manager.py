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


class SkillStoreConfig(DBConfig):
    """Configuration for the skill store."""

    table_name: str = "skill_table"


class SkillStore(BaseModel):
    """Class to manage the skill store in a database."""

    skill_config: SkillStoreConfig = SkillStoreConfig()
    db_host: str = skill_config.db_host
    db_name: str = settings.db_name
    db_url: str = f"{skill_config.db_host}:{skill_config.db_port}/{db_name}"
    table_name: str = skill_config.table_name
    db_user: str = skill_config.db_user
    db_password: str = skill_config.db_password
    db_type: str = skill_config.db_type
    db_schema: str = skill_config.db_schema
    engine: Any = None
    metadata: Any = None
    skill_table: Any = None

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.engine = create_engine(
            f"{self.db_type}://{self.db_user}:{self.db_password}@{self.db_url}"
        )
        self.metadata = MetaData(schema=self.db_schema)
        self.skill_table = Table(
            self.table_name,
            self.metadata,
            Column("skill_id", Integer, primary_key=True, autoincrement=True),
            Column("skill_name", String, nullable=False),
            Column("description", String, nullable=False),
            Column("instructions", String),
            Column("references_data", String),
            Column("assets_data", String),
            Column("owner_id", String),
            Column("organization_id", String, nullable=False),
            Column("project_id", String, nullable=False),
            Column("is_public", String, default="false"),
            Column("created_at", String),
            Column("updated_at", String),
            Column("status", String),
            UniqueConstraint(
                "skill_name",
                "organization_id",
                "project_id",
                name="unique_skill_name_per_org_proj",
            ),
        )

    def create_table(self):
        """Create the skill metadata table in the database."""
        if not inspect(self.engine).has_table(self.table_name):
            self.metadata.create_all(self.engine)
            logger.info(f"Skill store table '{self.table_name}' created successfully.")
        else:
            logger.info(f"Skill store table '{self.table_name}' already exists.")

    def get_list_skills(self, skills_query: Any) -> Any:
        """Get a list of skills from the database."""
        with self.engine.connect() as conn:
            result = conn.execute(skills_query)
            return result.fetchall()

    def delete_skill(self, skill_id: int) -> dict:
        """Delete a skill from the database."""
        delete_query = self.skill_table.delete().where(
            self.skill_table.c.skill_id == skill_id
        )
        with self.engine.begin() as conn:
            result = conn.execute(delete_query)
            if result.rowcount == 0:
                return {
                    "status": 404,
                    "message": f"Skill {skill_id} not found",
                }
            return {"status": 200, "message": "Skill deleted successfully"}


# DDL déplacé dans helpers/store_migrations.ensure_store_tables(),
# appelé au boot : importer ce module ne doit pas toucher la base.
skill_store = SkillStore()
