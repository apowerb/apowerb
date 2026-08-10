"""Unit tests for apowerb.artifacts.upload_mirror.

mirror_as_input_artifact is the additive write /bi/upload-csv and
/rag/index-files branch onto: same key scheme as routers/files.py
(artifacts/{app_name}/{session-or-_shared}/input/...), best-effort so a
failure here never fails the upload it mirrors.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest


class TestMirrorWritesInputArtifact:
    @pytest.mark.asyncio
    async def test_writes_via_save_input_artifact_when_s3_configured(self):
        from apowerb.artifacts.upload_mirror import mirror_as_input_artifact

        fake_save = AsyncMock(return_value=0)
        with patch(
            "apowerb.artifacts.upload_mirror.is_s3_artifact_storage_configured",
            return_value=True,
        ), patch(
            "apowerb.artifacts.upload_mirror.S3ArtifactService"
        ) as fake_service_cls:
            fake_service_cls.return_value.save_input_artifact = fake_save

            await mirror_as_input_artifact(
                app_name="agent1164",
                session_id="session_123",
                filename="report.csv",
                data=b"a,b\n1,2\n",
                content_type="text/csv",
                source="bi",
            )

        fake_save.assert_awaited_once_with(
            app_name="agent1164",
            session_id="session_123",
            filename="report.csv",
            data=b"a,b\n1,2\n",
            content_type="text/csv",
        )

    @pytest.mark.asyncio
    async def test_resolves_shared_scope_when_session_is_none(self):
        from apowerb.artifacts.upload_mirror import mirror_as_input_artifact

        fake_save = AsyncMock(return_value=0)
        with patch(
            "apowerb.artifacts.upload_mirror.is_s3_artifact_storage_configured",
            return_value=True,
        ), patch(
            "apowerb.artifacts.upload_mirror.S3ArtifactService"
        ) as fake_service_cls:
            fake_service_cls.return_value.save_input_artifact = fake_save

            await mirror_as_input_artifact(
                app_name="bi-thaink2.com",
                session_id=None,
                filename="dataset.csv",
                data=b"x",
                content_type="text/csv",
                source="bi",
            )

        assert fake_save.await_args.kwargs["session_id"] == "_shared"


class TestMirrorIsNoopWithoutS3:
    @pytest.mark.asyncio
    async def test_does_nothing_when_s3_not_configured(self):
        from apowerb.artifacts.upload_mirror import mirror_as_input_artifact

        with patch(
            "apowerb.artifacts.upload_mirror.is_s3_artifact_storage_configured",
            return_value=False,
        ), patch(
            "apowerb.artifacts.upload_mirror.S3ArtifactService"
        ) as fake_service_cls:
            await mirror_as_input_artifact(
                app_name="agent1164",
                session_id=None,
                filename="report.csv",
                data=b"x",
                content_type="text/csv",
                source="bi",
            )

        fake_service_cls.assert_not_called()


class TestMirrorIsBestEffort:
    @pytest.mark.asyncio
    async def test_swallows_failure_and_logs_error(self, caplog):
        from apowerb.artifacts.upload_mirror import mirror_as_input_artifact

        fake_save = AsyncMock(side_effect=RuntimeError("S3 unreachable"))
        with patch(
            "apowerb.artifacts.upload_mirror.is_s3_artifact_storage_configured",
            return_value=True,
        ), patch(
            "apowerb.artifacts.upload_mirror.S3ArtifactService"
        ) as fake_service_cls:
            fake_service_cls.return_value.save_input_artifact = fake_save

            with caplog.at_level(logging.ERROR):
                await mirror_as_input_artifact(
                    app_name="agent1164",
                    session_id=None,
                    filename="report.csv",
                    data=b"x",
                    content_type="text/csv",
                    source="rag",
                )

        assert any(
            record.levelno == logging.ERROR and "report.csv" in record.message
            for record in caplog.records
        )
