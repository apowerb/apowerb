"""tool_create_chart DB-connection auto-detection must tolerate real SQL layout.

Regression: the old check required a literal trailing space ("select "), so a
model emitting "SELECT\\n  ..." (newline after the verb — extremely common) was
treated as non-SQL, skipped auto-detection, and produced a source-less chart
that returned 422 on /data. Proven live with Mistral on th2demo.
"""

from __future__ import annotations

from apowerb.tools_store.portfolio.business_intelligence import _looks_like_sql


class TestLooksLikeSql:
    def test_select_followed_by_newline(self):
        assert _looks_like_sql("SELECT\n    v.a, SUM(v.b)\nFROM th2demo.ventes v") is True

    def test_leading_whitespace_and_with_cte(self):
        assert _looks_like_sql("   WITH t AS (SELECT 1) SELECT * FROM t") is True

    def test_select_followed_by_space_or_star(self):
        assert _looks_like_sql("SELECT * FROM t") is True
        assert _looks_like_sql("select id from t") is True

    def test_show_and_describe(self):
        assert _looks_like_sql("SHOW TABLES") is True
        assert _looks_like_sql("describe ventes") is True

    def test_non_sql_is_rejected(self):
        assert _looks_like_sql("hello, please make a chart") is False
        assert _looks_like_sql("") is False
        assert _looks_like_sql("selection of products") is False  # word boundary
