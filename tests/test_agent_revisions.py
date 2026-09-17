"""Agent revisions: what an update overwrites must stay restorable.

Until now the ``agents`` table held one current row per agent, so an UPDATE
destroyed the previous definition — instruction, model, tools, sub-agents —
with no way back short of a database restore.

These tests run against a real SQLite engine rather than fakes: the tables are
the store's own, the writes go through SQLAlchemy, and every assertion reads
the database back. That matters here because the defect being fixed is about
*what survives in storage*, which a captured ``.values(...)`` call cannot show.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import StaticPool

from apowerb.core import agent_main
from apowerb.schema.agent_schema import AgentCreateSchema

OWNER = "u@example.com"
OTHER = "someone-else@example.com"


def _sqlite_engine():
    """An in-memory engine that answers to whichever schema the store uses.

    ``DB_SCHEMA`` decides it: "public" by default (Postgres in production),
    empty on the CI bench, where the tables carry no schema at all. SQLite
    reaches a named schema through an ATTACHed database, so ``public`` is
    attached unconditionally — harmless when nothing is qualified with it.
    StaticPool keeps the single connection alive, so everything written
    survives between ``engine.begin()`` blocks.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture
def store(monkeypatch):
    """The real AgentStore, pointed at SQLite, with its tables created."""
    engine = _sqlite_engine()
    agent_store = agent_main.agent_store
    monkeypatch.setattr(agent_store, "engine", engine)
    agent_store.metadata.create_all(engine)
    # Materialising the ADK module on disk is out of scope here.
    monkeypatch.setattr(agent_main, "create_agent_module", lambda **k: None)
    return agent_store


def _insert_agent(store, **overrides):
    """Seed one stored agent, as the columns actually hold it."""
    row = {
        "agent_id": 1,
        "agent_name": "a1",
        "agent_model": "gemini-2.5-flash",
        "agent_model_params": json.dumps({}),
        "agent_description": "the first description",
        "agent_instruction": "version 1 instruction",
        "agent_tools": json.dumps(["tool_a"]),
        "agent_type": "llm",
        "sub_agents": json.dumps([]),
        "input_schema": json.dumps(None),
        "output_schema": json.dumps(None),
        "organization_id": "example.com",
        "project_id": "p1",
        "owner_id": OWNER,
        "created_at": "2026-09-17 10:00:00",
        "updated_at": "2026-09-17 10:00:00",
        "status": "active",
    }
    row.update(overrides)
    with store.engine.begin() as conn:
        conn.execute(store.agent_table.insert().values(**row))
    return row


def _payload(**extra):
    """Build the schema object exactly as the router hands it over."""
    base = {
        "agent_name": "a1",
        "agent_model": "gemini-2.5-flash",
        "agent_instruction": "version 2 instruction",
        "agent_type": "llm",
        "agent_description": "the second description",
    }
    parsed = AgentCreateSchema(**dict(base, **extra))
    return parsed.model_copy(
        update={"owner_id": OWNER, "organization_id": "example.com"}
    )


def _revisions(store):
    with store.engine.begin() as conn:
        rows = conn.execute(
            select(store.revision_table).order_by(store.revision_table.c.revision_id)
        ).fetchall()
    return [r._asdict() for r in rows]


def _stored_agent(store, agent_id=1):
    with store.engine.begin() as conn:
        row = conn.execute(
            select(store.agent_table).where(
                store.agent_table.c.agent_id == agent_id
            )
        ).fetchone()
    return row._asdict() if row else None


def test_an_update_archives_the_definition_it_overwrites(store):
    _insert_agent(store)

    agent_main.update_agent(1, _payload(), user_id=OWNER)

    revisions = _revisions(store)
    assert len(revisions) == 1, "the overwritten definition was not archived"
    archived = json.loads(revisions[0]["payload"])
    assert archived["agent_instruction"] == "version 1 instruction"
    assert archived["agent_description"] == "the first description"
    assert revisions[0]["reason"] == "update"
    assert revisions[0]["revised_by"] == OWNER


def test_restoring_a_revision_puts_the_previous_definition_back(store):
    _insert_agent(store)
    agent_main.update_agent(1, _payload(), user_id=OWNER)
    assert _stored_agent(store)["agent_instruction"] == "version 2 instruction"

    revision_id = _revisions(store)[0]["revision_id"]
    agent_main.restore_agent_revision(1, revision_id, user_id=OWNER)

    restored = _stored_agent(store)
    assert restored["agent_instruction"] == "version 1 instruction"
    assert restored["agent_description"] == "the first description"
    assert json.loads(restored["agent_tools"]) == ["tool_a"]


def test_restoring_archives_the_state_it_replaces(store):
    """A rollback must itself be reversible, or it is just another overwrite."""
    _insert_agent(store)
    agent_main.update_agent(1, _payload(), user_id=OWNER)
    agent_main.restore_agent_revision(1, _revisions(store)[0]["revision_id"], user_id=OWNER)

    revisions = _revisions(store)
    assert len(revisions) == 2
    assert revisions[1]["reason"] == "restore"
    assert json.loads(revisions[1]["payload"])["agent_instruction"] == (
        "version 2 instruction"
    )


def test_the_archive_never_holds_a_cleartext_api_key(store, monkeypatch):
    """Revisions are taken from the stored row, so secrets stay encrypted."""
    key = Fernet.generate_key()
    monkeypatch.setattr("apowerb.helpers.encryptor.fernet", Fernet(key))
    secret = "sk-super-secret-value"
    encrypted = Fernet(key).encrypt(secret.encode()).decode()
    _insert_agent(
        store,
        agent_model_params=json.dumps({"model_api_key": encrypted}),
    )

    agent_main.update_agent(1, _payload(), user_id=OWNER)

    payload = _revisions(store)[0]["payload"]
    assert secret not in payload
    assert encrypted in payload


def test_a_foreign_owner_can_neither_list_nor_restore(store):
    _insert_agent(store)
    agent_main.update_agent(1, _payload(), user_id=OWNER)
    revision_id = _revisions(store)[0]["revision_id"]

    assert agent_main.list_agent_revisions(1, user_id=OTHER) == []
    with pytest.raises(Exception) as excinfo:
        agent_main.restore_agent_revision(1, revision_id, user_id=OTHER)
    assert getattr(excinfo.value, "status_code", None) == 404
    assert _stored_agent(store)["agent_instruction"] == "version 2 instruction"


def test_listing_hides_the_payload_but_names_what_changed(store):
    _insert_agent(store)
    agent_main.update_agent(1, _payload(), user_id=OWNER)

    listed = agent_main.list_agent_revisions(1, user_id=OWNER)
    assert len(listed) == 1
    entry = listed[0]
    assert entry["agent_name"] == "a1"
    assert entry["reason"] == "update"
    assert "payload" not in entry, "the raw row carries encrypted secrets"
    assert "agent_instruction" in entry["changed_fields"]


def test_an_existing_agents_table_still_gets_the_revisions_table(monkeypatch):
    """The DDL trap: create_all was skipped whenever ``agents`` already existed.

    Every production database already has ``agents``, so a new table added to
    the same MetaData would never have been created there — the feature would
    have worked in tests and in a fresh container, and only there.
    """
    engine = _sqlite_engine()
    agent_store = agent_main.agent_store
    monkeypatch.setattr(agent_store, "engine", engine)
    # A database that predates this change: ``agents`` exists, nothing else.
    agent_store.agent_table.create(engine)

    agent_store.create_table()

    from sqlalchemy import inspect as sa_inspect

    # Le schema effectif depend de DB_SCHEMA : "public" en production, vide
    # (donc aucun schema) sur le banc SQLite de la CI. On interroge celui que
    # le store utilise vraiment, sinon le test ne prouve rien dans l une des
    # deux configurations.
    schema = agent_store.agent_table.schema
    tables = sa_inspect(engine).get_table_names(schema=schema)
    assert "agent_revisions" in tables
