import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    # async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from th2agent.helpers.database_connection import DBConfig
from th2agent.configs.settings import get_settings

settings = get_settings()
db_url = DBConfig().get_db_url()

metadata = MetaData(schema=settings.db_schema)
Base = declarative_base(metadata=metadata)


class DatabaseSessionManager:
    def __init__(self, host: str, engine_kwargs: dict[str, Any] = {}):
        self._engine = create_async_engine(
            host,
            connect_args={"server_settings": {"jit": "off"}},
            pool_pre_ping=True,  # Test connections before using them
            pool_size=5,
            max_overflow=10,
            pool_recycle=300,  # Recycle connections after 5 minutes
            **engine_kwargs
        )
        # self._sessionmaker = async_sessionmaker(autocommit=False, bind=self._engine)
        self._sessionmaker = sessionmaker(
            autocommit=False, bind=self._engine, class_=AsyncSession
        )

    async def close(self):
        if self._engine is None:
            raise Exception("DatabaseSessionManager is not initialized")
        await self._engine.dispose()

        self._engine = None
        self._sessionmaker = None

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        if self._engine is None:
            raise Exception("DatabaseSessionManager is not initialized")

        async with self._engine.begin() as connection:
            try:
                yield connection
            except Exception:
                await connection.rollback()
                raise

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise Exception("DatabaseSessionManager is not initialized")

        session = self._sessionmaker()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @property
    def engine(self):
        return self._engine


sessionmanager = DatabaseSessionManager(db_url, {"echo": settings.echo_sql})


async def get_db():
    async with sessionmanager.session() as session:
        yield session
