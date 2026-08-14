"""Unit tests for evaluation/run_service.list_owned_agents.

Feeds two screens with opposite needs. `admin_sees_all=True` is
Supervision, where crossing accounts is the job. `admin_sees_all=False` is
Evaluations, where it had produced a product screen listing all 133 agents
on the platform, each next to its owner's email address. The flag has no
default, so these tests always name the rule they are exercising.
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
        result = await list_owned_agents(
            _user(email="me@example.com"), admin_sees_all=True
        )

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
        result = await list_owned_agents(_user(role="admin"), admin_sees_all=True)

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
        await list_owned_agents(_user(email="me@example.com"), admin_sees_all=True)

    with_only_columns_result = store.agent_table.select.return_value.with_only_columns.return_value
    with_only_columns_result.where.assert_called_once()
    store.get_list_agents.assert_called_once_with(with_only_columns_result.where.return_value)


@pytest.mark.asyncio
async def test_owner_with_no_agents_gets_empty_list():
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = []
        result = await list_owned_agents(
            _user(email="me@example.com"), admin_sees_all=True
        )

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
        result = await list_owned_agents(
            _user(role="ADMIN", email="me@example.com"), admin_sees_all=True
        )

    assert [owner for _, _, owner in result] == [
        "me@example.com",
        "other@example.com",
        None,
    ]


@pytest.mark.asyncio
async def test_admin_is_filtered_like_anyone_else_when_admin_sees_all_is_false():
    """The Evaluations screen's rule.

    An administrator asking for their own agents must get the owner filter,
    the same as a regular user. Without it the screen listed every agent on
    the platform with each owner's email address on the card.
    """
    with patch("apowerb.core.agent_main.agent_store") as store:
        store.get_list_agents.return_value = [(1, "Mine", "me@example.com")]
        result = await list_owned_agents(
            _user(role="ADMIN", email="me@example.com"), admin_sees_all=False
        )

    assert result == [(1, "Mine", "me@example.com")]
    with_only_columns_result = store.agent_table.select.return_value.with_only_columns.return_value
    with_only_columns_result.where.assert_called_once()
    store.get_list_agents.assert_called_once_with(
        with_only_columns_result.where.return_value
    )


@pytest.mark.asyncio
async def test_the_rule_must_be_stated():
    """No default: a new route cannot inherit cross-account visibility by
    forgetting to think about it.
    """
    with pytest.raises(TypeError):
        await list_owned_agents(_user(role="ADMIN"))
