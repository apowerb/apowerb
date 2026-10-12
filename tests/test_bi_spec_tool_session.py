"""Outils agent BI v2 dans leur vrai contexte d'exécution : appelés depuis une
boucle asyncio active (ADK), ils calculent dans un thread doté de sa propre
boucle. Le pool de connexions partagé est lié à la boucle FastAPI ; le moteur
doit donc lire les jeux de données par une session propre à l'outil."""
import asyncio
import contextlib
import uuid

import apowerb.bi.data._bi_storage as storage
import apowerb.helpers.database as database
import apowerb.tools_store.portfolio.bi_dashboard_tools as t
from tests.helpers.bi_spec_data import weekly_sales

OWNER = "alice@example.com"


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Row:
    def __init__(self, key):
        self.config = {"s3_key": key}


class _ToolSession:
    def __init__(self, key):
        self.key = key
        self.calls = 0

    async def execute(self, _stmt):
        self.calls += 1
        return _Rows([_Row(self.key)])


class _SharedPoolOnOtherLoop:
    """Le sessionmanager de l'application vu depuis la boucle d'un outil."""

    def session(self):
        raise RuntimeError("Task got Future attached to a different loop")


class _Store:
    def __init__(self):
        self.saved = {}

    async def save(self, dashboard):
        self.saved[dashboard.id] = dashboard
        return dashboard

    async def get(self, dashboard_id):
        return self.saved.get(dashboard_id)


def test_stock_dashboard_tool_reads_its_dataset_on_first_compute(monkeypatch):
    key = f"bi/data/{uuid.uuid4()}.csv"  # unique: no dataset cache from another test
    csv = weekly_sales(weeks=156).to_csv(index=False).encode()
    tool_session = _ToolSession(key)
    store = _Store()

    @contextlib.asynccontextmanager
    async def tool_get_session():
        yield tool_session

    @contextlib.asynccontextmanager
    async def dashboard_store(owner):
        yield store

    monkeypatch.setattr(database, "sessionmanager", _SharedPoolOnOtherLoop())
    monkeypatch.setattr(t, "_get_session", tool_get_session)
    monkeypatch.setattr(t, "_dashboard_store", dashboard_store)
    monkeypatch.setattr(t, "_agent_owner", lambda: OWNER)
    monkeypatch.setattr(storage, "read_file", lambda k: csv if k == key else None)

    async def agent_turn():  # ADK calls sync tools while its loop is running
        return t.tool_bi_create_stock_dashboard(dataset_id=str(uuid.uuid4()), horizon=4)

    out = asyncio.run(agent_turn())

    assert out["success"] is True, out
    assert out["errors"] == {}, out["errors"]
    assert tool_session.calls >= 1
    kpis = [c for c in out["components"] if c["type"] == "kpi"]
    assert len(kpis) == 6 and all("value" in k for k in kpis)
