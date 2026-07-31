"""Tests d'isolation owner pour tools_store/tools_helpers.py.

Vérifie qu'un utilisateur authentifié ne peut pas :
- lister les tool_configs d'un autre tenant,
- lire (fetch) un tool_config d'un autre tenant,
- supprimer (delete) un tool_config d'un autre tenant,
- mettre à jour (update) un tool_config d'un autre tenant.

Ces tests utilisent un moteur SQLite en mémoire et reconstruisent la table
``tool_configs`` avec les mêmes colonnes que le store de production.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
)


USER_A = "alice@example.com"
USER_B = "bob@example.com"


# ---------------------------------------------------------------------------
# Fixture : environnement minimal (clé Fernet) + store SQLite
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _test_encrypt_key(monkeypatch):
    """Force a valid Fernet key so encrypt/decrypt round-trip works.

    Restores the original Fernet instance on teardown so later tests that
    rely on the real encrypt_key keep working.
    """
    from apowerb.helpers import encryptor as enc_mod

    original_fernet = enc_mod.fernet
    key = Fernet.generate_key().decode()
    enc_mod.fernet = Fernet(key.encode())
    try:
        yield
    finally:
        enc_mod.fernet = original_fernet


def _build_sqlite_store():
    """Create an in-memory SQLite table matching tool_configs schema."""
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    table = Table(
        "tool_configs",
        metadata,
        Column("tool_config_id", Integer, primary_key=True, autoincrement=True),
        Column("tool_config_name", String),
        Column("tool_name", String),
        Column("tool_config_params", String),
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
    )
    metadata.create_all(engine)
    return engine, table


@pytest.fixture
def fake_store():
    """Patch tool_config_store + encryptor so helpers use an in-memory SQLite."""
    import importlib

    # Prevent the real ToolConfigStore from connecting to Postgres at import
    # time: stub `create_table` before the helpers module is loaded/reloaded.
    from apowerb.tools_store import tool_config as tc_mod

    engine, table = _build_sqlite_store()

    class _FakeStore:
        def __init__(self):
            self.engine = engine
            self.tool_config_table = table

        def get_list_tool_configs(self, query):
            with self.engine.connect() as conn:
                return conn.execute(query).fetchall()

        def delete_tool_config(self, tool_config_id: int, owner_id: str) -> dict:
            """Proxy that mimics production signature — owner_id is mandatory."""
            q = table.delete().where(
                (table.c.tool_config_id == tool_config_id)
                & (table.c.owner_id == owner_id)
            )
            with self.engine.begin() as conn:
                result = conn.execute(q)
                if result.rowcount == 0:
                    return {
                        "status": 404,
                        "message": f"Tool config {tool_config_id} not found",
                    }
                return {"status": 200, "message": "Tool config deleted successfully"}

    with patch.object(tc_mod.ToolConfigStore, "create_table", lambda self: None):
        # Import helpers once (module is cached after first test)
        from apowerb.tools_store import tools_helpers as th  # noqa: WPS433

        fake = _FakeStore()
        with patch.object(th, "tool_config_store", fake):
            yield th, fake, engine, table


def _insert(engine, table, owner_id: str, name: str, params: dict | None = None) -> int:
    """Insert a row and return its ID. Params are stored already-encrypted."""
    from apowerb.helpers.encryptor import encrypt_value_in_dict

    if params is None:
        params = {}
    encrypted = encrypt_value_in_dict(dict(params), values_to_encrypt=list(params.keys()))
    with engine.begin() as conn:
        result = conn.execute(
            table.insert().values(
                tool_config_name=name,
                tool_name="integration.my_tool",
                tool_config_params=json.dumps(encrypted),
                tool_category="integration",
                tool_config_type="active",
                owner_id=owner_id,
                project_id="thaink2",
                organization_id="example.com",
                created_at="2026-01-01",
                updated_at="2026-01-01",
                status="active",
            )
        )
        return int(result.inserted_primary_key[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListUserToolConfigsIsolation:
    def test_list_returns_only_current_user_configs(self, fake_store):
        th, _fake, engine, table = fake_store
        id_a = _insert(engine, table, USER_A, "alice-tool")
        id_b = _insert(engine, table, USER_B, "bob-tool")

        result = th.list_user_tool_configs(user_id=USER_A)
        ids = {c["tool_config_id"] for c in result}
        assert f"tool_config{id_a}" in ids
        assert f"tool_config{id_b}" not in ids


class TestFetchToolConfigIsolation:
    def test_fetch_foreign_config_returns_not_found(self, fake_store):
        th, _fake, engine, table = fake_store
        id_b = _insert(engine, table, USER_B, "bob-tool", {"SECRET": "bob"})

        res = th.fetch_tool_configs(f"tool_config{id_b}", owner_id=USER_A)

        assert res.get("status") in (404, 200)
        assert "not found" in (res.get("message") or "").lower()
        # Surtout : aucune fuite de secret
        assert "tool_config_params" not in res or not res.get("tool_config_params")

    def test_fetch_own_config_succeeds(self, fake_store):
        th, _fake, engine, table = fake_store
        id_a = _insert(engine, table, USER_A, "alice-tool", {"SECRET": "alice"})

        res = th.fetch_tool_configs(f"tool_config{id_a}", owner_id=USER_A)

        assert res.get("tool_config_id") == f"tool_config{id_a}"
        assert res["tool_config_params"]["SECRET"] == "alice"

    def test_fetch_system_tool_is_allowed(self, fake_store):
        """SuperAgents share 'system'-owned tools — any user can read them."""
        th, _fake, engine, table = fake_store
        id_sys = _insert(engine, table, "system", "shared-tool")

        res = th.fetch_tool_configs(f"tool_config{id_sys}", owner_id=USER_A)

        assert res.get("tool_config_id") == f"tool_config{id_sys}"


class TestDeleteToolConfigIsolation:
    def test_delete_foreign_config_rejected(self, fake_store):
        th, _fake, engine, table = fake_store
        id_b = _insert(engine, table, USER_B, "bob-tool")

        res = th.delete_tool_config(f"tool_config{id_b}", owner_id=USER_A)
        assert res.get("status") == 404

        # Le tool de Bob existe toujours
        with engine.connect() as conn:
            rows = conn.execute(
                table.select().where(table.c.tool_config_id == id_b)
            ).fetchall()
        assert len(rows) == 1

    def test_delete_own_config_succeeds(self, fake_store):
        th, _fake, engine, table = fake_store
        id_a = _insert(engine, table, USER_A, "alice-tool")

        res = th.delete_tool_config(f"tool_config{id_a}", owner_id=USER_A)
        assert res.get("status") == 200

        with engine.connect() as conn:
            rows = conn.execute(
                table.select().where(table.c.tool_config_id == id_a)
            ).fetchall()
        assert len(rows) == 0


class TestUpdateToolConfigIsolation:
    def test_update_foreign_config_rejected(self, fake_store):
        th, _fake, engine, table = fake_store
        id_b = _insert(engine, table, USER_B, "bob-tool", {"SECRET": "bob"})

        res = th.update_tool_config(
            f"tool_config{id_b}",
            {"tool_config_name": "hacked-name"},
            owner_id=USER_A,
        )
        assert res.get("status") == 404

        # Nom original inchangé
        with engine.connect() as conn:
            rows = conn.execute(
                table.select().where(table.c.tool_config_id == id_b)
            ).fetchall()
        assert rows[0]._mapping["tool_config_name"] == "bob-tool"

    def test_update_own_config_succeeds(self, fake_store):
        th, _fake, engine, table = fake_store
        id_a = _insert(engine, table, USER_A, "alice-tool")

        res = th.update_tool_config(
            f"tool_config{id_a}",
            {"tool_config_name": "alice-tool-renamed"},
            owner_id=USER_A,
        )
        assert res.get("status") == 200

        with engine.connect() as conn:
            rows = conn.execute(
                table.select().where(table.c.tool_config_id == id_a)
            ).fetchall()
        assert rows[0]._mapping["tool_config_name"] == "alice-tool-renamed"


class TestLoadToolConfigParamsIsolation:
    def test_load_foreign_params_returns_none(self, fake_store):
        th, _fake, engine, table = fake_store
        id_b = _insert(engine, table, USER_B, "bob-tool", {"REFRESH_TOKEN": "oauth-secret"})

        tool_name, params = th.load_tool_config_params(
            f"tool_config{id_b}", owner_id=USER_A
        )
        assert params is None or params == {}

    def test_load_own_params_ok(self, fake_store):
        th, _fake, engine, table = fake_store
        id_a = _insert(
            engine, table, USER_A, "alice-tool", {"REFRESH_TOKEN": "alice-secret"}
        )

        tool_name, params = th.load_tool_config_params(
            f"tool_config{id_a}", owner_id=USER_A
        )
        assert params == {"REFRESH_TOKEN": "alice-secret"}
