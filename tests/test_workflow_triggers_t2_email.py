"""``email`` (T2) — kind 1/5 : un mail reçu (Outlook/Gmail) lance un run.

La réception RÉELLE d'un webhook Microsoft Graph / Gmail Pub/Sub n'est pas
exercée ici (nécessite un abonnement live) : voir la note dans
``routers/webhook_handlers/outlook.py``/``gmail.py`` pour le point
d'insertion exact et sa portée. Ce fichier couvre ce qui EST testable sans
réseau : le filtrage (provider, from_filter, subject_filter,
insensible à la casse) et le déclenchement (owner, kind, payload).
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_email_triggers as wet
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"
BOB = "bob@other.fr"


def _email_graph(provider="outlook", from_filter=None, subject_filter=None):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "email",
                    "provider": provider,
                    "from_filter": from_filter,
                    "subject_filter": subject_filter,
                },
            }
        ],
        "edges": [],
    }


# --- matches_email_trigger (pur) --------------------------------------------


def test_no_filters_always_matches():
    cfg = {"provider": "outlook", "from_filter": None, "subject_filter": None}
    assert wet.matches_email_trigger(cfg, from_addr="x@y.fr", subject="hello") is True


def _from(flt, addr):
    return wet.matches_email_trigger(
        {"from_filter": flt, "subject_filter": None}, from_addr=addr, subject="s"
    )


def test_from_filter_address_is_an_exact_case_insensitive_match():
    assert _from("Boss@Company.com", "boss@company.com") is True
    assert _from("boss@company.com", "Boss Name <BOSS@company.com>") is True
    # Revue 22/09 : une sous-chaîne non ancrée laissait passer ces adresses.
    assert _from("boss@company.com", "boss@company.com.evil.com") is False
    assert _from("boss@company.com", "xboss@company.com") is False


def test_from_filter_domain_matches_the_domain_and_its_subdomains_only():
    for flt in ("@acme.fr", "ACME.fr"):
        assert _from(flt, "jean@acme.fr") is True
        assert _from(flt, "Jean <jean@eu.acme.fr>") is True
        assert _from(flt, "jean@acme.fr.evil.com") is False
        assert _from(flt, "jean@notacme.fr") is False
        assert _from(flt, "jean@other.fr") is False


def test_from_filter_never_matches_an_unparsable_sender():
    assert _from("@acme.fr", "") is False
    assert _from("@acme.fr", "not an address") is False


def test_subject_filter_is_case_insensitive_substring():
    cfg = {"from_filter": None, "subject_filter": "invoice"}
    assert (
        wet.matches_email_trigger(cfg, from_addr="a@b.fr", subject="Your INVOICE #42")
        is True
    )
    assert wet.matches_email_trigger(cfg, from_addr="a@b.fr", subject="hello") is False


def test_both_filters_must_match():
    cfg = {"from_filter": "acme.fr", "subject_filter": "invoice"}
    assert (
        wet.matches_email_trigger(cfg, from_addr="a@acme.fr", subject="invoice") is True
    )
    assert (
        wet.matches_email_trigger(cfg, from_addr="a@acme.fr", subject="hello") is False
    )
    assert (
        wet.matches_email_trigger(cfg, from_addr="a@other.fr", subject="invoice")
        is False
    )


# --- dispatch_email_triggers -------------------------------------------------


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


def _seed_integration(engine, *, user_id: int, email: str, provider: str) -> None:
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
    # "user"/"integrations" : lus par compute_active (integration_missing),
    # jamais migrés par ce module (voir workflow_triggers.integration_present)
    # -- le test doit les fournir lui-même, comme en prod (ORM ailleurs).
    wt._ext_metadata.create_all(engine)
    _seed_integration(engine, user_id=1, email=ALICE, provider="microsoft_outlook")
    _seed_integration(engine, user_id=2, email=ALICE, provider="google_gmail")
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
        return f"run-{workflow_id}", None


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    return rec


def _publish(wf, owner):
    wm.update_workflow(
        wf["workflow_id"],
        owner_id=owner,
        expected_version=wf["version"],
        status="published",
    )


_ENVELOPE = {
    "from": "jean@acme.fr",
    "to": "me@apowerb.io",
    "subject": "Your invoice #42",
    "body": "...",
    "received_at": "2026-09-22T10:00:00Z",
    "attachments": [{"name": "f.pdf", "size": 100, "content_type": "application/pdf"}],
}


async def test_dispatches_to_matching_published_trigger(recorder):
    wf = wm.create_workflow(
        owner_id=ALICE, name="W", graph=_email_graph(provider="outlook")
    )
    _publish(wf, ALICE)

    run_ids = await wet.dispatch_email_triggers(
        provider="outlook", owner_id=ALICE, envelope=_ENVELOPE
    )

    assert run_ids == [f"run-{wf['workflow_id']}"]
    assert recorder.calls[0]["kind"] == "email"
    assert recorder.calls[0]["payload"] == _ENVELOPE


async def test_skips_a_trigger_for_a_different_provider(recorder):
    wf = wm.create_workflow(
        owner_id=ALICE, name="W", graph=_email_graph(provider="gmail")
    )
    _publish(wf, ALICE)

    run_ids = await wet.dispatch_email_triggers(
        provider="outlook", owner_id=ALICE, envelope=_ENVELOPE
    )

    assert run_ids == []
    assert recorder.calls == []


async def test_skips_a_trigger_whose_filter_does_not_match(recorder):
    wf = wm.create_workflow(
        owner_id=ALICE,
        name="W",
        graph=_email_graph(provider="outlook", subject_filter="receipt"),
    )
    _publish(wf, ALICE)

    run_ids = await wet.dispatch_email_triggers(
        provider="outlook", owner_id=ALICE, envelope=_ENVELOPE
    )

    assert run_ids == []


async def test_never_dispatches_to_another_owners_trigger(recorder):
    wf = wm.create_workflow(
        owner_id=BOB, name="W", graph=_email_graph(provider="outlook")
    )
    _publish(wf, BOB)

    run_ids = await wet.dispatch_email_triggers(
        provider="outlook", owner_id=ALICE, envelope=_ENVELOPE
    )

    assert run_ids == []


async def test_unpublished_trigger_is_not_active_so_not_dispatched(recorder):
    wm.create_workflow(owner_id=ALICE, name="W", graph=_email_graph(provider="outlook"))

    run_ids = await wet.dispatch_email_triggers(
        provider="outlook", owner_id=ALICE, envelope=_ENVELOPE
    )

    assert run_ids == []
