from pydantic import BaseModel
from th2agent.configs.settings import get_settings

settings = get_settings()


class DBConfig(BaseModel):
    """Configuration for the agent store."""

    db_host: str = settings.db_host
    db_port: int = settings.db_port
    db_name: str = settings.db_name
    db_url: str = f"{settings.db_host}:{settings.db_port}"
    db_user: str = settings.db_user
    db_password: str = settings.db_password
    db_type: str = settings.db_type  # e.g., 'sqlite', 'postgresql', etc.
    db_schema: str = settings.db_schema

    def get_db_url(self, mode: str = "async") -> str:
        """Constructs the database URL from settings."""
        if mode == "sync":
            # Synchronous driver (psycopg2)
            return f"{self.db_type}+psycopg2://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_schema}"
        else:
            # Asynchronous driver (asyncpg)
            return f"{self.db_type}+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
