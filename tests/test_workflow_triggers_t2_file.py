"""``file`` (T2) — kind 4/5 : un nouveau fichier dans un dossier surveillé
lance un run par fichier, via le ``flow_scheduler`` existant.

L'appel réseau réel (Microsoft Graph / Google Drive) n'est PAS exercé ici :
``poll_file_trigger`` prend un ``list_files`` injecté, c'est la seule
fonction non couverte (voir sa note dans ``core.workflow_file_triggers``).
Ce fichier couvre : la déduplication (id + modified_at), l'amorçage sans
déclenchement au premier passage, et le déclenchement un run par fichier.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_file_triggers as wft
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"


def _file_graph(provider="onedrive", interval_min=5):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "file",
                    "provider": provider,
                    "folder_id": "root",
                    "folder_label": "Documents",
                    "interval_min": interval_min,
                },
            }
        ],
        "edges": [],
    }


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


def _seed_integration(engine, *, user_id, email, provider):
    with engine.begin() as conn:
        conn.execute(wt._user_table.insert().values(user_id=user_id, email=email))
        conn.execute(
            wt._integration_table.insert().values(
                id=user_id, user_id=user_id, provider=provider
            )
        )


@pytest.fixture(autouse=True)
def store(monkeypatch):
    engine = _sqlite_engine()
    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)
    monkeypatch.setattr(wft.seen_file_store, "engine", engine)
    wft.seen_file_store.metadata.create_all(engine)
    wt._ext_metadata.create_all(engine)
    _seed_integration(engine, user_id=1, email=ALICE, provider="microsoft_onedrive")
    return engine


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, workflow_id, owner_id, kind, detail, payload):
        self.calls.append(
            dict(
                workflow_id=workflow_id,
                owner_id=owner_id,
                kind=kind,
                detail=detail,
                payload=payload,
            )
        )
        return f"run-{payload['id']}", None


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    return rec


def _publish(wf):
    wm.update_workflow(
        wf["workflow_id"],
        owner_id=ALICE,
        expected_version=wf["version"],
        status="published",
    )


def _row_for(wf):
    t = wt.workflow_trigger_store.trigger_table
    with wt.workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(
            t.select().where(t.c.workflow_id == wf["workflow_id"])
        ).fetchone()
    return dict(row._mapping)


_F1 = {
    "id": "f1",
    "name": "a.pdf",
    "path": "/a.pdf",
    "url": "https://x/a",
    "size": 10,
    "modified_at": "2026-01-01T00:00:00Z",
}
_F2 = {
    "id": "f2",
    "name": "b.pdf",
    "path": "/b.pdf",
    "url": "https://x/b",
    "size": 20,
    "modified_at": "2026-01-01T00:00:00Z",
}


# --- dedupe_new_files (pur) --------------------------------------------------


def test_all_files_are_new_when_nothing_was_seen_before():
    new = wft.dedupe_new_files(seen={}, current=[_F1, _F2])
    assert {f["id"] for f in new} == {"f1", "f2"}


def test_unchanged_file_is_not_reported_again():
    seen = {"f1": _F1["modified_at"]}
    new = wft.dedupe_new_files(seen=seen, current=[_F1])
    assert new == []


def test_file_with_changed_modified_at_is_reported():
    seen = {"f1": "2020-01-01T00:00:00Z"}
    new = wft.dedupe_new_files(seen=seen, current=[_F1])
    assert new == [_F1]


# --- poll_file_trigger --------------------------------------------------------


async def test_first_poll_baselines_without_firing(recorder):
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_file_graph())
    _publish(wf)
    row = _row_for(wf)

    async def _lister(**kwargs):
        return [_F1, _F2]

    fired = await wft.poll_file_trigger(row, list_files=_lister)

    assert fired == 0
    assert recorder.calls == []
    assert wft.get_seen_files(wf["workflow_id"]) == {
        "f1": _F1["modified_at"],
        "f2": _F2["modified_at"],
    }


async def test_second_poll_fires_only_the_new_file(recorder):
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_file_graph())
    _publish(wf)
    row = _row_for(wf)

    async def _first(**kwargs):
        return [_F1]

    await wft.poll_file_trigger(row, list_files=_first)

    async def _second(**kwargs):
        return [_F1, _F2]

    fired = await wft.poll_file_trigger(row, list_files=_second)

    assert fired == 1
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["kind"] == "file"
    assert recorder.calls[0]["payload"] == {
        "id": "f2",
        "name": "b.pdf",
        "path": "/b.pdf",
        "url": "https://x/b",
        "size": 20,
        "modified_at": _F2["modified_at"],
    }


async def test_a_modified_existing_file_refires(recorder):
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_file_graph())
    _publish(wf)
    row = _row_for(wf)

    async def _first(**kwargs):
        return [_F1]

    await wft.poll_file_trigger(row, list_files=_first)

    updated = {**_F1, "modified_at": "2026-02-01T00:00:00Z"}

    async def _second(**kwargs):
        return [updated]

    fired = await wft.poll_file_trigger(row, list_files=_second)

    assert fired == 1
    assert recorder.calls[0]["payload"]["modified_at"] == "2026-02-01T00:00:00Z"


async def test_integration_missing_polls_zero_and_does_not_raise(recorder):
    wf = wm.create_workflow(
        owner_id=ALICE, name="W", graph=_file_graph(provider="google_drive")
    )
    _publish(wf)
    row = _row_for(wf)

    async def _lister(**kwargs):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("list_files ne doit pas être appelé sans intégration")

    fired = await wft.poll_file_trigger(row, list_files=_lister)

    assert fired == 0
    assert recorder.calls == []


# --- due_file_triggers / réservation atomique --------------------------------


async def test_due_file_triggers_returns_only_past_due_rows():
    from datetime import datetime, timedelta, timezone

    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_file_graph())
    _publish(wf)

    due_now = wt.due_file_triggers(datetime.now(timezone.utc) + timedelta(hours=1))
    assert any(r["workflow_id"] == wf["workflow_id"] for r in due_now)

    due_past = wt.due_file_triggers(datetime.now(timezone.utc) - timedelta(hours=1))
    assert not any(r["workflow_id"] == wf["workflow_id"] for r in due_past)
