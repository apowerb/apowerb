"""A conversation's title belongs to the session, not to one browser.

The chat used to generate a title and keep it in local storage. Opened from
another machine, or after a cleared cache, the conversation went back to being
named by its id — which is what the evaluation screen showed.

The title now lives in ADK's own `state` column, written with the jsonb merge
operator, the same semantics ADK uses (`storage_session.state | state_delta`).
That is the property these tests pin: neither write destroys the other.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from apowerb.routers.adk_runner import (
    SESSION_TITLE_KEY,
    SetSessionTitleRequest,
    set_session_title,
)

_MODULE = "apowerb.routers.adk_runner"


def _user(email="user@example.com"):
    return MagicMock(email=email, role="USER")


def _db(rowcount=1):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    return db


async def _call(db, title="Ventes par département", user=None, session="s1"):
    with patch(f"{_MODULE}.get_agent_folder_name", return_value="agent1201"):
        return await set_session_title(
            agent_name="agent1201",
            user_id="user@example.com",
            session_id=session,
            request=SetSessionTitleRequest(title=title),
            db=db,
            current_user=user or _user(),
        )


class TestWrite:
    @pytest.mark.asyncio
    async def test_the_title_is_merged_never_assigned(self):
        """An assignment would drop whatever ADK had already stored."""
        db = _db()

        await _call(db)

        statement = str(db.execute.await_args.args[0])
        assert "||" in statement, statement
        # The lethal shape: SET state = '{...}'
        assert "SET state = COALESCE" in statement

    @pytest.mark.asyncio
    async def test_the_key_is_namespaced(self):
        """`output_key="title"` on an agent must not overwrite a conversation."""
        assert SESSION_TITLE_KEY == "apowerb_title"

        db = _db()
        await _call(db, title="Rapport hebdo")

        params = db.execute.await_args.args[1]
        assert SESSION_TITLE_KEY in params["patch"]
        assert "Rapport hebdo" in params["patch"]

    @pytest.mark.asyncio
    async def test_it_commits(self):
        db = _db()
        await _call(db)
        db.commit.assert_awaited_once()


class TestRefusals:
    @pytest.mark.asyncio
    async def test_an_empty_title_is_refused(self):
        db = _db()
        with pytest.raises(HTTPException) as excinfo:
            await _call(db, title="   ")
        assert excinfo.value.status_code == 400
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unknown_session_is_a_404_not_a_silent_success(self):
        """No row touched means the session is not there — say so."""
        db = _db(rowcount=0)
        with pytest.raises(HTTPException) as excinfo:
            await _call(db, session="does-not-exist")
        assert excinfo.value.status_code == 404
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_another_users_session_is_refused(self):
        db = _db()
        with pytest.raises(HTTPException):
            await _call(db, user=_user("someone-else@example.com"))
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_very_long_title_is_cut_not_rejected(self):
        """A long first message must not cost the user their title."""
        db = _db()
        result = await _call(db, title="x" * 500)
        assert len(result["title"]) == 200
