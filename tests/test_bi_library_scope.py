"""A BI upload has to reach the Artifacts tab, not just the bucket.

`/bi/upload-csv` mirrors its file under a `bi-<organization>` folder, because
BI has no agent to attach it to. The library sweep, however, only ever visited
the agents a user owns (`agent_table` filtered on `owner_id`), so those files
were written correctly and stayed invisible — the exact failure this feature
was meant to remove.

The ownership test for a `bi-` folder is the BI table itself: every upload
writes a row carrying both the owner and the organization.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.routers.artifacts import _owned_bi_scopes


def _db_returning(*organization_ids):
    db = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = [(value,) for value in organization_ids]
    db.execute = AsyncMock(return_value=result)
    return db


class TestOwnedBiScopes:
    @pytest.mark.asyncio
    async def test_maps_each_organization_to_its_folder(self):
        db = _db_returning("acme", "globex")

        scopes = await _owned_bi_scopes(db, "someone@example.com")

        assert scopes == {"bi-acme": "BI — acme", "bi-globex": "BI — globex"}

    @pytest.mark.asyncio
    async def test_a_user_without_bi_gets_nothing(self):
        scopes = await _owned_bi_scopes(_db_returning(), "someone@example.com")

        assert scopes == {}

    @pytest.mark.asyncio
    async def test_blank_organizations_are_dropped(self):
        """A blank id would sweep the prefix `bi-`, i.e. every organization."""
        db = _db_returning("acme", None, "")

        scopes = await _owned_bi_scopes(db, "someone@example.com")

        assert scopes == {"bi-acme": "BI — acme"}

    @pytest.mark.asyncio
    async def test_a_broken_bi_table_does_not_empty_the_library(self):
        """The screen still has to show every agent artifact."""
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))

        scopes = await _owned_bi_scopes(db, "someone@example.com")

        assert scopes == {}

    @pytest.mark.asyncio
    async def test_the_failure_is_logged_not_swallowed(self, caplog):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))

        with caplog.at_level("ERROR"):
            await _owned_bi_scopes(db, "someone@example.com")

        assert "[ARTIFACTS]" in caplog.text


class TestLibrarySweepIncludesBi:
    @pytest.mark.asyncio
    async def test_bi_folders_are_swept_alongside_owned_agents(self):
        from apowerb.routers import artifacts as module

        with patch.object(module, "_s3_artifacts_active", return_value=True), patch.object(
            module, "_owned_agents", return_value={"agent12": "Support"}
        ), patch.object(
            module,
            "_owned_bi_scopes",
            AsyncMock(return_value={"bi-acme": "BI — acme"}),
        ), patch.object(module, "build_library", return_value=[]) as fake_build:
            user = MagicMock(email="someone@example.com")
            await module.list_artifact_library(current_user=user, db=MagicMock())

        swept = fake_build.call_args.args[0]
        assert swept == {"agent12": "Support", "bi-acme": "BI — acme"}
