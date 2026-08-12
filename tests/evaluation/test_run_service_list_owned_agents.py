"""Unit tests for evaluation/run_service.list_owned_agents.

Feeds `GET /evaluations/agents`: every agent the caller owns (id + display
name), admin unrestricted -- same ownership rule and same synchronous
`agent_store.agent_table` connection as `owned_agent_ids`, just carrying the
name along so the router never has to look agents up one at a time.
"""

from unittest.mock import MagicMock, patch

import pytest

from apowerb.evaluation.run_service import list_owned_agents


def _user(email="owner@example.com", role="user"):
    u = MagicMock()
    u.email = email
    u.role = role
    return u


@pytest.mark.asyncio
async def test_regular_user_gets_only_their_agents():
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = [
            (1, "Send_mail", "me@example.com"),
            (2, "Triage", "me@example.com"),
        ]
        result = await list_owned_agents(_user(email="me@example.com"))

    assert result == [
        (1, "Send_mail", "me@example.com"),
        (2, "Triage", "me@example.com"),
    ]


@pytest.mark.asyncio
async def test_admin_is_unrestricted():
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = [
            (1, "A", "someone@example.com"),
            (2, "B", "other@example.com"),
            (3, "C", None),
        ]
        result = await list_owned_agents(_user(role="admin"))

    assert len(result) == 3
    # The query must not carry an owner_id filter for an admin: `.where`
    # is never called on the `with_only_columns(...)` result.
    with_only_columns_result = store.agent_table.select.return_value.with_only_columns.return_value
    with_only_columns_result.where.assert_not_called()
    store.get_list_agents.assert_called_once_with(with_only_columns_result)


@pytest.mark.asyncio
async def test_regular_user_query_filters_by_owner_id():
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = []
        await list_owned_agents(_user(email="me@example.com"))

    with_only_columns_result = store.agent_table.select.return_value.with_only_columns.return_value
    with_only_columns_result.where.assert_called_once()
    store.get_list_agents.assert_called_once_with(with_only_columns_result.where.return_value)


@pytest.mark.asyncio
async def test_owner_with_no_agents_gets_empty_list():
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = []
        result = await list_owned_agents(_user(email="me@example.com"))

    assert result == []


@pytest.mark.asyncio
async def test_the_owner_comes_back_with_each_agent():
    """An admin is served every agent on the platform — 186 on dev, of which
    12 belong to the person looking. Without the owner the screen can only
    claim they are all theirs, which is exactly what it did.
    """
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = [
            (1, "Mine", "me@example.com"),
            (2, "Someone else's", "other@example.com"),
            (3, "Orphan", None),
        ]
        result = await list_owned_agents(_user(role="ADMIN", email="me@example.com"))

    assert [owner for _, _, owner in result] == [
        "me@example.com",
        "other@example.com",
        None,
    ]
