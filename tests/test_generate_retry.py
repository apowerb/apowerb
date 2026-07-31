"""Robustness of _agent_generate_sql: tolerant extraction + one corrective retry.

Local models (Mistral on OVH) often wrap SQL in prose/markdown or emit a
non-SELECT on the first try. The generator must extract cleanly and retry once
with the failure as feedback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import apowerb.tools_store.portfolio.text_to_sql as t2s


def _resp(content):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    return r


_SCHEMA = {
    "schema": "public",
    "db_type": "postgresql",
    "tables": {
        "orders": {
            "columns": [{"column_name": "id", "data_type": "integer"}],
            "primary_keys": ["id"],
            "foreign_keys": [],
            "sample_data": [],
        }
    },
}


def _fake_state():
    s = MagicMock()
    s.model = "mistral/whatever"
    s.api_base = "http://endpoint"
    s.api_key = "k"
    s.db_type = "postgresql"
    return s


class TestGenerateRetry:
    def test_extracts_and_no_retry_when_first_is_valid(self):
        with patch.object(t2s, "_get_state", return_value=_fake_state()), patch.object(
            t2s.litellm, "completion",
            return_value=_resp("```sql\nSELECT id FROM orders\n```"),
        ) as comp:
            sql = t2s._agent_generate_sql("agent1", "list orders", _SCHEMA)
        assert sql == "SELECT id FROM orders"
        assert comp.call_count == 1

    def test_retries_once_on_prose_then_recovers(self):
        responses = [
            _resp("Sure! Here is the query you asked for: please run it."),
            _resp("SELECT id FROM orders"),
        ]
        with patch.object(t2s, "_get_state", return_value=_fake_state()), patch.object(
            t2s.litellm, "completion", side_effect=responses,
        ) as comp:
            sql = t2s._agent_generate_sql("agent1", "list orders", _SCHEMA)
        assert sql == "SELECT id FROM orders"
        assert comp.call_count == 2
        # The retry message must carry corrective feedback.
        retry_msgs = comp.call_args_list[1].kwargs["messages"]
        assert retry_msgs[-1]["role"] == "user"
        assert "ONLY the SQL" in retry_msgs[-1]["content"]
