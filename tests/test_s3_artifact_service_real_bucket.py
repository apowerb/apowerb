"""End-to-end proof against the real dev bucket (th2agent-dev).

Farid migrated 8 real objects into it, in the exact key layout this service
imposes, so S3ArtifactService.load_artifact / list_artifact_keys /
list_versions must find them without any mocking:

    artifacts/agent1164/session_1785833154778/output/fizzbuzz.py/0/fizzbuzz.py
    artifacts/agent1164/session_1785833154778/output/rapport.html/0/rapport.html
    artifacts/agent1234/session_1785927283476/output/rapport_IA_medecine_v2.html/0/...
    artifacts/agent1237/session_1785927619686/output/rapport_IA_militaire.html/0/...

READ-ONLY: this module must never write or delete anything in the bucket —
two of these objects are real client demo reports.

Requires real S3 credentials in the environment (S3_BUCKET_NAME and
friends); skipped otherwise, same convention as the other `integration`
tests in this suite that need a live dependency.
"""

from __future__ import annotations

import json

import pytest

from apowerb.artifacts.s3_artifact_service import S3ArtifactService
from apowerb.configs.settings import get_settings

pytestmark = pytest.mark.integration

_USER_ID = "irrelevant-for-session-scoped-artifacts"


def _s3_configured() -> bool:
    s = get_settings()
    return bool(s.s3_bucket_name and s.s3_access_key and s.s3_endpoint)


skip_without_s3 = pytest.mark.skipif(
    not _s3_configured(), reason="S3 credentials not set in this environment"
)


@pytest.fixture
def service():
    return S3ArtifactService()


@skip_without_s3
class TestRealMigratedObjects:
    @pytest.mark.asyncio
    async def test_loads_fizzbuzz_artifact_from_agent1164_session(self, service):
        part = await service.load_artifact(
            app_name="agent1164",
            user_id=_USER_ID,
            filename="fizzbuzz.py",
            session_id="session_1785833154778",
        )
        assert part is not None
        payload = json.loads(part.inline_data.data.decode("utf-8"))
        assert payload["filename"] == "fizzbuzz.py"
        assert "code" in payload

    @pytest.mark.asyncio
    async def test_loads_rapport_html_from_agent1164_session(self, service):
        part = await service.load_artifact(
            app_name="agent1164",
            user_id=_USER_ID,
            filename="rapport.html",
            session_id="session_1785833154778",
        )
        assert part is not None
        payload = json.loads(part.inline_data.data.decode("utf-8"))
        assert payload["filename"] == "rapport.html"

    @pytest.mark.asyncio
    async def test_loads_medecine_report_from_agent1234_session(self, service):
        part = await service.load_artifact(
            app_name="agent1234",
            user_id=_USER_ID,
            filename="rapport_IA_medecine_v2.html",
            session_id="session_1785927283476",
        )
        assert part is not None

    @pytest.mark.asyncio
    async def test_loads_militaire_report_from_agent1237_session(self, service):
        part = await service.load_artifact(
            app_name="agent1237",
            user_id=_USER_ID,
            filename="rapport_IA_militaire.html",
            session_id="session_1785927619686",
        )
        assert part is not None

    @pytest.mark.asyncio
    async def test_lists_both_artifact_keys_in_agent1164_session(self, service):
        keys = await service.list_artifact_keys(
            app_name="agent1164",
            user_id=_USER_ID,
            session_id="session_1785833154778",
        )
        assert "fizzbuzz.py" in keys
        assert "rapport.html" in keys

    @pytest.mark.asyncio
    async def test_lists_version_0_for_fizzbuzz(self, service):
        versions = await service.list_versions(
            app_name="agent1164",
            user_id=_USER_ID,
            filename="fizzbuzz.py",
            session_id="session_1785833154778",
        )
        assert versions == [0]

    @pytest.mark.asyncio
    async def test_get_artifact_version_reports_canonical_s3_uri(self, service):
        av = await service.get_artifact_version(
            app_name="agent1164",
            user_id=_USER_ID,
            filename="fizzbuzz.py",
            session_id="session_1785833154778",
        )
        assert av is not None
        assert av.version == 0
        assert av.canonical_uri.endswith(
            "artifacts/agent1164/session_1785833154778/output/fizzbuzz.py/0/fizzbuzz.py"
        )
