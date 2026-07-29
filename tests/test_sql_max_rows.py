"""TDD - Levier 2 : cap resultats SQL a SQL_MAX_ROWS.

Tests ecrits AVANT l'implementation. Objectif : limiter les resultats
SQL a 150 lignes max (configurable via SQL_MAX_ROWS) pour ne pas
saturer le contexte 32k OVHcloud.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


class TestSqlMaxRows:
    """Tests du cap SQL_MAX_ROWS dans _execute_and_format."""

    def _run_execute_and_format(self, rows_count: int, max_rows_env: str | None = None):
        """Helper : simule un SELECT et retourne le dict resultat."""
        # Construction d'un curseur mock qui retourne rows_count lignes
        fake_rows = [{"col": f"val_{i}"} for i in range(rows_count)]

        # Mock du curseur avec attribut description pour columns
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = fake_rows
        mock_cursor.description = [("col",)]

        mock_conn = MagicMock()

        # SQL SELECT trivial
        sql = "SELECT col FROM table"

        env_patch = {}
        if max_rows_env is not None:
            env_patch["SQL_MAX_ROWS"] = max_rows_env

        with patch.dict(os.environ, env_patch, clear=False):
            # On a besoin de reimporter pour que os.getenv soit relu
            import importlib
            import th2agent.tools_store.portfolio.database as db_mod
            importlib.reload(db_mod)

            # Patcher le curseur pour que fetchall retourne nos fausses lignes
            # et que _serialize_row retourne la ligne telle quelle
            with patch.object(db_mod, '_serialize_row', side_effect=lambda r: r):
                # On doit aussi patcher cursor.execute pour ne pas crasher
                mock_cursor.execute = MagicMock()
                mock_cursor.fetchall.return_value = fake_rows
                result = db_mod._execute_and_format(mock_conn, mock_cursor, sql, "postgresql")

        return result

    def test_200_rows_truncated_to_150(self, caplog):
        """200 lignes -> tronque a 150 + warning loggue."""
        import logging
        with caplog.at_level(logging.WARNING, logger="th2agent.tools_store.portfolio.database"):
            with patch.dict(os.environ, {"SQL_MAX_ROWS": "150"}, clear=False):
                result = self._run_execute_and_format(200, max_rows_env="150")

        assert result["row_count"] == 150
        assert len(result["data"]) == 150
        # Verifier qu'un warning a ete emis
        assert any("truncat" in r.message.lower() or "SQL" in r.message
                   for r in caplog.records), f"Aucun warning SQL truncation : {caplog.records}"

    def test_37_rows_unchanged(self):
        """37 lignes (< 150) -> inchange."""
        with patch.dict(os.environ, {"SQL_MAX_ROWS": "150"}, clear=False):
            result = self._run_execute_and_format(37, max_rows_env="150")

        assert result["row_count"] == 37
        assert len(result["data"]) == 37

    def test_default_max_rows_is_150(self):
        """Sans SQL_MAX_ROWS, la valeur par defaut est 150."""
        # Supprimer SQL_MAX_ROWS si present
        env_without = {k: v for k, v in os.environ.items() if k != "SQL_MAX_ROWS"}
        with patch.dict(os.environ, env_without, clear=True):
            result = self._run_execute_and_format(200)

        assert result["row_count"] == 150

    def test_custom_max_rows_via_env(self):
        """SQL_MAX_ROWS=50 -> tronque a 50."""
        with patch.dict(os.environ, {"SQL_MAX_ROWS": "50"}, clear=False):
            result = self._run_execute_and_format(100, max_rows_env="50")

        assert result["row_count"] == 50

    def test_write_operations_not_affected(self):
        """Les operations INSERT/UPDATE/DELETE ne sont pas affectees par SQL_MAX_ROWS."""
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 5
        mock_conn = MagicMock()

        import importlib
        import th2agent.tools_store.portfolio.database as db_mod
        importlib.reload(db_mod)

        with patch.dict(os.environ, {"SQL_MAX_ROWS": "3"}, clear=False):
            result = db_mod._execute_and_format(mock_conn, mock_cursor, "INSERT INTO t VALUES (1)", "postgresql")

        assert result["rows_affected"] == 5
        assert "data" not in result

    def test_invalid_sql_max_rows_uses_fallback_150(self):
        """SQL_MAX_ROWS=abc (non-numerique) -> fallback 150, pas de crash (ValueError)."""
        result = self._run_execute_and_format(200, max_rows_env='abc')
        assert result['row_count'] == 150, (
            f'fallback attendu 150, obtenu {result["row_count"]}'
        )
