"""Tests for apowerb.models.LlmUsage — importable, correct columns/table."""
from __future__ import annotations


def test_llm_usage_importable_with_expected_table_name():
    from apowerb.models import LlmUsage

    assert LlmUsage.__tablename__ == "llm_usage"


def test_llm_usage_has_expected_columns():
    from apowerb.models import LlmUsage

    columns = {c.name for c in LlmUsage.__table__.columns}
    expected = {
        "id",
        "created_at",
        "agent_id",
        "agent_name",
        "owner_id",
        "session_id",
        "invocation_source",
        "model",
        "input_tokens",
        "output_tokens",
        "thoughts_tokens",
        "cached_tokens",
        "total_tokens",
    }
    assert expected <= columns


def test_llm_usage_id_is_primary_key():
    from apowerb.models import LlmUsage

    assert LlmUsage.__table__.c.id.primary_key


def test_llm_usage_has_agent_id_created_at_index():
    from apowerb.models import LlmUsage

    index_columns = [
        tuple(c.name for c in idx.columns) for idx in LlmUsage.__table__.indexes
    ]
    assert ("agent_id", "created_at") in index_columns


def test_llm_usage_has_owner_id_created_at_index():
    """Les 3 requetes du endpoint /api/usage/summary filtrent sur
    (owner_id, created_at) -- sans index dedie elles tombent sur un scan
    complet de la table (seul ix_llm_usage_agent_created existe)."""
    from apowerb.models import LlmUsage

    index_columns = [
        tuple(c.name for c in idx.columns) for idx in LlmUsage.__table__.indexes
    ]
    assert ("owner_id", "created_at") in index_columns


def test_llm_usage_default_token_counts_are_zero_capable():
    from apowerb.models import LlmUsage

    for col_name in ("thoughts_tokens", "cached_tokens"):
        col = LlmUsage.__table__.c[col_name]
        assert col.default is not None
        assert col.default.arg == 0
