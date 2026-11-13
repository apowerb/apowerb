"""Bout en bout : « mail reçu → Doc Intelligence → Jev AR / pas AR → base ».

Ce qui est RÉEL : l'API (``POST /workflows/defs`` puis ``/simulate-email``
en multipart), le stockage des pièces jointes et leurs refs, le moteur de
workflow, les services du runtime (``services_for``), l'outil OCR jusqu'à
son appel HTTP, l'outil Jev jusqu'à son client, et l'écriture dans un
PostgreSQL jetable (initdb local, TCP seul, supprimé en fin de module).

Ce qui est SIMULÉ : le service Azure Document Intelligence (HTTP 202 +
Operation-Location, texte lu dans le PDF envoyé avec pypdf), le modèle Jev
(règle par mots-clés) et la configuration de connexion (``fetch_tool_configs``).
Données : ``tests/fixtures/workflow_mail_ar`` — entièrement fictives.
Ignoré si les binaires PostgreSQL (initdb, pg_ctl) sont absents.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

FIXTURES = Path(__file__).parent / "fixtures" / "workflow_mail_ar"
sys.path.insert(0, str(FIXTURES))
import make_pdfs

ALICE, BOB = "alice@exemple.test", "bob@exemple.test"
CONNECTION = "tool_config7"
AR_WORDS = ("accusé de réception", "accusons réception", "acknowledg", "confirmation de commande")

pytestmark = pytest.mark.skipif(
    not (shutil.which("initdb") and shutil.which("pg_ctl")),
    reason="PostgreSQL local (initdb/pg_ctl) absent",
)


# --- PostgreSQL jetable -----------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    import psycopg2

    data = tmp_path_factory.mktemp("pgdata")
    port = _free_port()
    # Sans locale valide, le postmaster macOS refuse de démarrer
    # (« devenu multithreadé lors du démarrage »).
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    subprocess.run(
        ["initdb", "-D", str(data), "-U", "demo", "--auth=trust", "-E", "UTF8", "--locale=C"],
        check=True,
        capture_output=True,
        env=env,
    )
    opts = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
    subprocess.run(
        ["pg_ctl", "-D", str(data), "-o", opts, "-l", str(data / "log"), "-w", "start"],
        check=True,
        capture_output=True,
        env=env,
    )
    params = {
        "DB_HOST": "127.0.0.1",
        "DB_PORT": str(port),
        "DB_NAME": "postgres",
        "DB_USER": "demo",
        "DB_PASSWORD": "",
        "DB_ALLOWED_OPS": "SELECT,INSERT",
    }
    conn = psycopg2.connect(host="127.0.0.1", port=port, dbname="postgres", user="demo")
    conn.autocommit = True
    conn.cursor().execute(
        """CREATE TABLE documents_recus (
            id serial PRIMARY KEY,
            expediteur text NOT NULL,
            sujet text NOT NULL,
            fichier text,
            classe text NOT NULL,
            extrait text,
            recu_le timestamptz DEFAULT now()
        )"""
    )
    try:
        yield params, conn
    finally:
        conn.close()
        subprocess.run(
            ["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"],
            check=False,
            capture_output=True,
            env=env,
        )
        shutil.rmtree(data, ignore_errors=True)


def _rows(conn):
    cur = conn.cursor()
    cur.execute("SELECT expediteur, sujet, fichier, classe, extrait FROM documents_recus ORDER BY id")
    return cur.fetchall()


# --- Azure Document Intelligence et Jev simulés ----------------------------------


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_azure(monkeypatch):
    import httpx
    from pypdf import PdfReader

    monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "cle-fictive")
    monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "https://di.exemple.test")
    results, calls = {}, []

    def post(url, headers=None, json=None, timeout=None):
        assert url.startswith("https://di.exemple.test/documentintelligence/")
        assert headers["Ocp-Apim-Subscription-Key"] == "cle-fictive"
        data = base64.b64decode(json["base64Source"])
        text = PdfReader(io.BytesIO(data)).pages[0].extract_text()
        op = f"https://di.exemple.test/operations/{len(results)}"
        results[op] = text
        calls.append(op)
        return _Resp(202, headers={"Operation-Location": op})

    def get(url, headers=None, timeout=None):
        lines = [{"content": line} for line in results[url].splitlines()]
        return _Resp(
            200,
            {"status": "succeeded", "analyzeResult": {"modelId": "prebuilt-read", "pages": [{"pageNumber": 1, "lines": lines}]}},
        )

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return calls


@pytest.fixture
def fake_jev(monkeypatch):
    from apowerb.tools_store.portfolio import jev

    seen = []

    class Client:
        def decide(self, context, question):
            text = context["item"].lower()
            seen.append(text)
            ar = any(w in text for w in AR_WORDS)
            probs = {"ar": 0.96, "autre": 0.04} if ar else {"ar": 0.06, "autre": 0.94}
            return {"answers": {"label": {"choice": max(probs, key=probs.get), "probabilities": probs}}}

    monkeypatch.setattr(jev, "_client_or_error", lambda: (Client(), None))
    return seen


# --- API ------------------------------------------------------------------------


def _user(email):
    u = MagicMock()
    u.email, u.user_id, u.role = email, 1, "USER"
    return u


@pytest.fixture
def client(monkeypatch, tmp_path, pg):
    import apowerb.core.run_gate as gate
    from apowerb.auth.dependencies import get_current_user
    from apowerb.core import run_main
    from apowerb.core import workflow_main as wm
    from apowerb.core import workflow_triggers as wt
    from apowerb.routers import workflow_defs
    from apowerb.tools_store import tools_helpers

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)
    monkeypatch.setenv("TH2_ATTACHMENT_ROOT", str(tmp_path / "pj"))
    monkeypatch.setattr(run_main, "start_run", lambda **kw: kw["run_id"])
    monkeypatch.setattr(run_main, "finish_run", lambda *a, **k: None)

    async def _plan(owner):
        return None

    monkeypatch.setattr(gate, "resolve_owner_plan", _plan)
    params, _conn = pg

    def fetch(tool_config_id, owner_id, items_to_select="*"):
        if tool_config_id == CONNECTION and owner_id == ALICE:
            return {
                "tool_config_id": CONNECTION,
                "owner_id": ALICE,
                "tool_category": "database",
                "tool_config_params": dict(params),
            }
        return {"status": 404, "message": "not found"}

    monkeypatch.setattr(tools_helpers, "fetch_tool_configs", fetch)

    who = {"email": ALICE}
    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _user(who["email"])
    c = TestClient(app)
    c.who = who
    return c


def _graph():
    graph = json.loads((FIXTURES / "graph.json").read_text(encoding="utf-8"))
    for node in graph["nodes"]:
        if node["type"] == "db_write":
            node["config"]["connection"] = CONNECTION
    return graph


def _simulate(client, wid, mail):
    r = client.post(
        f"/api/workflows/defs/{wid}/simulate-email",
        data={"from": mail["from"], "subject": mail["subject"], "body": mail["body"]},
        files=[("files", (mail["file"], make_pdfs.pdf_bytes(mail["pdf"]), "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    return [json.loads(chunk[6:]) for chunk in r.text.split("\n\n") if chunk.startswith("data: ")]


@pytest.fixture
def workflow(client):
    r = client.post("/api/workflows/defs", json={"name": "Tri des AR (démo)", "graph": _graph()})
    assert r.status_code == 201, r.text
    assert client.get(f"/api/workflows/defs/{r.json()['workflow_id']}").json()["validation"]["valid"]
    return r.json()["workflow_id"]


def test_six_fictitious_mails_are_classified_and_only_ars_are_written(
    client, workflow, pg, fake_azure, fake_jev
):
    _params, conn = pg
    conn.cursor().execute("TRUNCATE documents_recus")
    mails = make_pdfs.mails()
    routes = {}
    for mail in mails:
        events = _simulate(client, workflow, mail)
        assert events[-1]["event"] == "done", events[-1]
        route = next(e["route"] for e in events if e["event"] == "route" and e["node_id"] == "tri")
        routes[mail["id"]] = route
        expected_out = ("AR enregistré : " if mail["expected"] == "ar" else "Pas un AR : ") + mail["subject"]
        assert events[-1]["output"] == expected_out
        trigger = next(e for e in events if e["event"] == "node_complete" and e["node_id"] == "mail")
        (att,) = trigger["output"]["attachments"]
        assert att["ref"].startswith("att_") and "path" not in att

    assert routes == {m["id"]: m["expected"] for m in mails}
    assert len(fake_azure) == 6 and len(fake_jev) == 6

    rows = _rows(conn)
    ars = [m for m in mails if m["expected"] == "ar"]
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (m["from"], m["subject"], m["file"], "AR") for m in ars
    ]
    assert all("COMMANDE" in r[4].upper() or "ORDER" in r[4].upper() for r in rows)


def test_hostile_subject_is_stored_verbatim_not_executed(client, workflow, pg, fake_azure, monkeypatch):
    from apowerb.tools_store.portfolio import jev

    class AlwaysAr:
        def decide(self, context, question):
            return {"answers": {"label": {"choice": "ar", "probabilities": {"ar": 0.99, "autre": 0.01}}}}

    monkeypatch.setattr(jev, "_client_or_error", lambda: (AlwaysAr(), None))
    _params, conn = pg
    conn.cursor().execute("TRUNCATE documents_recus")
    hostile = next(m for m in make_pdfs.mails() if "DROP TABLE" in m["subject"])
    events = _simulate(client, workflow, hostile)
    assert events[-1]["event"] == "done", events[-1]
    assert [r[1] for r in _rows(conn)] == [hostile["subject"]]


def test_azure_failure_stops_the_run_with_a_readable_error(client, workflow, pg, monkeypatch, fake_jev):
    import httpx

    monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "cle-fictive")
    monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "https://di.exemple.test")

    def post(url, headers=None, json=None, timeout=None):
        request = httpx.Request("POST", url)
        raise httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request, text="invalid key"))

    monkeypatch.setattr(httpx, "post", post)
    _params, conn = pg
    conn.cursor().execute("TRUNCATE documents_recus")
    events = _simulate(client, workflow, make_pdfs.mails()[0])
    err = next(e for e in events if e["event"] == "node_error")
    assert err["node_id"] == "pieces" and err["code"] == "document_failed"
    assert events[-1]["event"] == "error"
    assert fake_jev == [] and _rows(conn) == []


def test_another_user_cannot_simulate_on_my_workflow(client, workflow):
    client.who["email"] = BOB
    r = client.post(
        f"/api/workflows/defs/{workflow}/simulate-email",
        data={"from": "x@exemple.test"},
        files=[("files", ("a.pdf", b"%PDF", "application/pdf"))],
    )
    assert r.status_code == 404


def test_refused_file_type_is_422(client, workflow):
    r = client.post(
        f"/api/workflows/defs/{workflow}/simulate-email",
        data={"from": "x@exemple.test"},
        files=[("files", ("a.exe", b"MZ", "application/x-msdownload"))],
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "attachment_type"
