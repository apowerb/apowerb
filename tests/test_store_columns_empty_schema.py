"""An empty DB_SCHEMA means no schema when the API key and hub stores add their newer columns.

``ensure_columns()`` reflects the existing table to find which of the newer
columns it still lacks, then adds them. It passed ``schema=self.db_schema`` to
the inspector unchanged, so an empty ``DB_SCHEMA`` (the first leg of the CI
matrix) asked for schema ``""`` instead of no schema. The ``ALTER TABLE`` right
below already treats an empty schema as none, as do the sibling stores
(``db_schema or None``).

What is pinned here is the migration itself: a table created before the newer
columns existed has them after ``create_table()``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from apowerb.agent_store.api_key_store import ApiKeyStore
from apowerb.agent_store.hub_manager import HubStore


@pytest.mark.parametrize(
    ("store_cls", "newer_columns"),
    [
        (ApiKeyStore, {"model", "model_api_base"}),
        (
            HubStore,
            {
                "sub_agents_snapshot",
                "loop_max_iterations",
                "loop_exit_instruction",
                "agent_skills",
            },
        ),
    ],
)
def test_an_older_table_gets_the_newer_columns_with_an_empty_schema(
    store_cls, newer_columns
):
    store = store_cls(db_schema="")
    store.engine = create_engine("sqlite://", poolclass=StaticPool)
    with store.engine.begin() as conn:
        conn.execute(
            text(f'CREATE TABLE "{store.table_name}" (id INTEGER PRIMARY KEY)')
        )

    store.create_table()

    columns = {c["name"] for c in inspect(store.engine).get_columns(store.table_name)}
    assert newer_columns <= columns
