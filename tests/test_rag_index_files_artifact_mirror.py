"""POST /rag/index-files must mirror each uploaded file into the artifact
chain (kind=input), same key scheme as routers/files.py, without touching
where RAG actually reads from (local disk, uploads_dir()/{scope}/{filename}
-- tool_create_knowledge indexes that path, unchanged).

Option C: a known session_id scopes the mirror to
artifacts/{agent}/{session}/input/...; none falls back to
artifacts/{agent}/_shared/input/....

Mirroring is best-effort: a mirror failure must not turn a successful index
request into an error response.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.storage import s3 as s3_storage
from tests.helpers.fake_s3 import FakeS3Client

AGENT = "agent1164"
SESSION = "session_1785833154778"
USER = "dev@example.com"


class _FakeAgentStore:
    def __init__(self, agents: dict[int, dict]):
        self._agents = agents

        class _Col:
            def __eq__(self, other):
                return ("agent_id", other)

        class _C:
            agent_id = _Col()

        class _T:
            c = _C()

            def select(self):
                class _S:
                    def where(self_inner, cond):
                        self_inner.cond = cond
                        return self_inner

                return _S()

        self.agent_table = _T()

    def get_list_agents(self, query):
        target_id = query.cond[1]
        if target_id in self._agents:
            row = MagicMock()
            row._asdict = lambda: self._agents[target_id]
            return [row]
        return []


@pytest.fixture()
def rag_env(monkeypatch, tmp_path):
    # Import first, patch after: apowerb.core.knowledge_map does
    # `from apowerb.configs.paths import uploads_dir` at its own import
    # time. Patching apowerb.configs.paths.uploads_dir before this
    # package's first-ever import in the test session would make that
    # `from ... import` statement bind knowledge_map's uploads_dir
    # permanently to this test's tmp_path lambda -- a leak into every
    # later test module for the rest of the pytest process.
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers.rag import router

    fake = FakeS3Client()
    monkeypatch.setattr(s3_storage, "_get_s3_client", lambda: fake)

    # Indexation authenticates against the RAG API, which since #59 refuses
    # to invent a credential. Stand in for a configured install.
    monkeypatch.setenv("RAG_SERVICE_ACCOUNT_EMAIL", "service@example.com")
    monkeypatch.setenv("th2password", "test-password")

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_mode", "S3", raising=False)
    monkeypatch.setattr(settings, "s3_bucket_name", "test-bucket", raising=False)
    monkeypatch.setattr(settings, "s3_access_key", "AK", raising=False)
    monkeypatch.setattr(settings, "s3_access_key_secret", "SK", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint", "https://s3.example.com", raising=False)
    monkeypatch.setattr(settings, "s3_region", "gra", raising=False)
    monkeypatch.setattr(settings, "public_base_url", "https://dev.example.com", raising=False)
    # Only index_files' own bound name needs redirecting -- append_source
    # and rag_manager are mocked below, so nothing else in this test
    # resolves apowerb.configs.paths.uploads_dir.
    monkeypatch.setattr(
        "apowerb.routers.rag.index_files.uploads_dir", lambda: tmp_path, raising=False
    )

    agents = {1164: {"agent_id": 1164, "owner_id": USER, "agent_name": "dev-agent"}}
    fake_agent_store = _FakeAgentStore(agents)

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user():
        u = MagicMock()
        u.email = USER
        u.user_id = 1
        u.role = "USER"
        return u

    app.dependency_overrides[get_current_user] = _user

    # index_files binds tool_create_knowledge at import time, so patching it
    # on its defining module leaves the router calling the real one -- which
    # is how this test was quietly reaching the live RAG service until the
    # hardcoded credential went away.
    with patch("apowerb.core.agent_main.agent_store", fake_agent_store), patch(
        "apowerb.routers.rag.index_files.tool_create_knowledge",
        return_value={"status": "ok", "knowledge_id": "kb-1"},
    ), patch(
        "apowerb.routers.rag.append_source",
        return_value={"source_type": "file", "name": "n"},
    ), patch(
        "apowerb.routers.rag.rag_manager.register_knowledge",
        new=AsyncMock(return_value=None),
    ):
        yield TestClient(app), fake


class TestIndexFilesMirrorsInputArtifact:
    def test_writes_to_session_scope_when_session_id_given(self, rag_env):
        client, fake = rag_env
        resp = client.post(
            "/api/rag/index-files",
            data={"agent_id": AGENT, "session_id": SESSION},
            files={"files": ("doc.pdf", b"%PDF-1.4 content", "application/pdf")},
        )
        assert resp.status_code == 200, resp.text

        expected_key = f"artifacts/{AGENT}/{SESSION}/input/doc.pdf/0/doc.pdf"
        assert expected_key in fake._objects
        assert fake._objects[expected_key]["body"] == b"%PDF-1.4 content"

    def test_writes_to_shared_scope_when_session_id_absent(self, rag_env):
        client, fake = rag_env
        resp = client.post(
            "/api/rag/index-files",
            data={"agent_id": AGENT},
            files={"files": ("doc2.pdf", b"content", "application/pdf")},
        )
        assert resp.status_code == 200, resp.text

        expected_key = f"artifacts/{AGENT}/_shared/input/doc2.pdf/0/doc2.pdf"
        assert expected_key in fake._objects

    def test_indexation_still_succeeds_when_mirror_fails(self, rag_env):
        client, _ = rag_env
        with patch(
            "apowerb.routers.rag.index_files.mirror_as_input_artifact",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post(
                "/api/rag/index-files",
                data={"agent_id": AGENT},
                files={"files": ("doc3.pdf", b"content", "application/pdf")},
            )
        assert resp.status_code == 200, resp.text
