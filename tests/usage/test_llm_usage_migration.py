"""Tests TDD pour helpers/llm_usage_migration.ensure_llm_usage_table.

La table `llm_usage` existe deja en DEV/PROD (creee avant l'ajout de
l'index owner_created) : le create_all/CREATE TABLE IF NOT EXISTS ne
repassera jamais dessus. L'index doit donc etre cree via un
CREATE INDEX IF NOT EXISTS execute inconditionnellement, meme quand la
branche "table already exists" est prise.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch


def _mock_engine(table_exists: bool):
    conn = MagicMock()
    executed_sql: list[str] = []

    def execute(stmt, *args, **kwargs):
        executed_sql.append(str(stmt))
        return MagicMock()

    conn.execute.side_effect = execute
    conn.commit = MagicMock()

    @contextmanager
    def connect_ctx():
        yield conn

    engine = MagicMock()
    engine.connect = connect_ctx
    engine.dispose = MagicMock()

    inspector = MagicMock()
    inspector.get_table_names.return_value = ["llm_usage"] if table_exists else []

    return engine, inspector, executed_sql


class TestEnsureLlmUsageTableOwnerCreatedIndex:
    def test_index_created_when_table_already_exists(self):
        """Cas DEV/PROD reel : la table preexiste -> l'index owner_created
        doit quand meme etre cree."""
        from apowerb.helpers import llm_usage_migration

        engine, inspector, executed_sql = _mock_engine(table_exists=True)

        with (
            patch.object(llm_usage_migration, "create_engine", return_value=engine),
            patch.object(llm_usage_migration, "inspect", return_value=inspector),
        ):
            llm_usage_migration.ensure_llm_usage_table()

        joined = " ".join(executed_sql)
        assert "ix_llm_usage_owner_created" in joined
        assert "IF NOT EXISTS" in joined

    def test_index_created_when_table_is_freshly_created(self):
        """Table absente -> creation table + index agent_created existant +
        nouvel index owner_created, tous dans le meme passage."""
        from apowerb.helpers import llm_usage_migration

        engine, inspector, executed_sql = _mock_engine(table_exists=False)

        with (
            patch.object(llm_usage_migration, "create_engine", return_value=engine),
            patch.object(llm_usage_migration, "inspect", return_value=inspector),
        ):
            llm_usage_migration.ensure_llm_usage_table()

        joined = " ".join(executed_sql)
        assert "CREATE TABLE IF NOT EXISTS" in joined
        assert "ix_llm_usage_agent_created" in joined
        assert "ix_llm_usage_owner_created" in joined
