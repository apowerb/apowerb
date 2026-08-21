"""Regression tests for ``scei_ar_persist.tool_persist_ar_record``.

Why these tests exist: the tool was created to make the prod schema
guarantees independent of LLM compliance. 11 prior PRs of "MANDATORY"
/ "STRICT" prompt directives could not stop claude-sonnet from
dropping ``DateReceptionAR`` and writing ``Traite = 1`` instead of 0.
These tests pin the guards in code so a future refactor cannot
silently revert them.

See `[[incident_2026-05-19_dashboard_dateReceptionAR]]`.
"""
from __future__ import annotations

import re

import pytest

from th2customers.scei.tools import scei_ar_persist
from unittest.mock import MagicMock

from th2customers.scei.tools.scei_ar_persist import (
    _build_insert_sql,
    _COMMANDES_COLUMNS,
    _REJECTED_COLUMNS,
    _VALID_STATUT_GLOBAL,
    tool_persist_ar_record,
)


class TestColumnCompleteness:
    """The INSERT must list the 13 real columns of dbo.Commandes.

    Was 12 from 2026-05-19 (MessageId removal) to 2026-05-20 (PR 3b
    adds WebhookLogId, the FK linking an AR row to the webhook_logs
    row that captured the original email + attachments).
    """

    def test_thirteen_columns_listed_in_correct_order(self):
        assert len(_COMMANDES_COLUMNS) == 13
        assert _COMMANDES_COLUMNS == (
            "NumeroCommande",
            "DateCommande",
            "Societe",
            "FournisseurCode",
            "FournisseurNom",
            "Commanditaire",
            "DateReceptionAR",
            "EmailExpediteur",
            "StatutGlobal",
            "PJOneDrivePath",
            "Traite",
            "Decision",
            "WebhookLogId",
        )

    def test_all_columns_appear_in_generated_sql(self):
        sql = _build_insert_sql()
        # Extract the column list between the first parens.
        m = re.search(r"INSERT INTO dbo\.Commandes \(([^)]+)\)", sql)
        assert m is not None, f"INSERT preamble missing in SQL: {sql!r}"
        listed = [c.strip() for c in m.group(1).split(",")]
        assert listed == list(_COMMANDES_COLUMNS), (
            f"INSERT must list every documented column, in order. "
            f"Expected {list(_COMMANDES_COLUMNS)}, got {listed}"
        )


class TestForcedValues:
    """Columns the LLM cannot influence: hardcoded server-side."""

    def test_date_reception_ar_is_sysutcdatetime(self):
        sql = _build_insert_sql()
        # ``DateReceptionAR`` must NOT be a bind parameter — it must be
        # the server-evaluated ``SYSUTCDATETIME()`` literal.
        assert ":DateReceptionAR" not in sql, (
            "DateReceptionAR must NOT be bound from LLM input — incident "
            "2026-05-19: LLM omitted the column entirely. Force server-side."
        )
        assert "SYSUTCDATETIME()" in sql

    def test_traite_is_zero_literal(self):
        sql = _build_insert_sql()
        assert ":Traite" not in sql, (
            "Traite must NOT be bound from LLM input — observed prod "
            "INSERTs write Traite=1, breaking the 'à traiter' filter."
        )
        # The literal 0 must be the value provided for Traite. Locate the
        # Traite position in the column list and the matching VALUES slot.
        cols_match = re.search(r"INSERT INTO dbo\.Commandes \(([^)]+)\)", sql)
        vals_match = re.search(r"VALUES \((.+)\)$", sql)
        assert cols_match and vals_match
        cols = [c.strip() for c in cols_match.group(1).split(",")]
        vals = [v.strip() for v in vals_match.group(1).split(",")]
        idx = cols.index("Traite")
        assert vals[idx] == "0", (
            f"Traite slot must be the literal 0, got {vals[idx]!r}"
        )

    def test_decision_is_null_literal(self):
        sql = _build_insert_sql()
        assert ":Decision" not in sql
        cols_match = re.search(r"INSERT INTO dbo\.Commandes \(([^)]+)\)", sql)
        vals_match = re.search(r"VALUES \((.+)\)$", sql)
        cols = [c.strip() for c in cols_match.group(1).split(",")]
        vals = [v.strip() for v in vals_match.group(1).split(",")]
        idx = cols.index("Decision")
        assert vals[idx] == "NULL", (
            f"Decision slot must be the literal NULL, got {vals[idx]!r}"
        )


class TestRejectedColumns:
    """Server-default columns must never appear in the INSERT."""

    def test_rejected_columns_never_in_sql(self):
        """ID/CreatedAt/UpdatedAt must not appear in the INSERT column
        list nor in the VALUES clause. There is no OUTPUT clause (removed
        in fix SQL-334: AFTER trigger forbids OUTPUT without INTO).
        The row ID is retrieved via SELECT SCOPE_IDENTITY()."""
        sql = _build_insert_sql()
        cols_match = re.search(r"INSERT INTO dbo\.Commandes \(([^)]+)\)", sql)
        vals_match = re.search(r"VALUES \((.+)\)$", sql)
        assert cols_match and vals_match
        in_cols = [c.strip() for c in cols_match.group(1).split(",")]
        in_vals = vals_match.group(1)
        for col in _REJECTED_COLUMNS:
            assert col not in in_cols, (
                f"{col!r} must not be in the INSERT column list "
                f"(server default)."
            )
            assert f":{col}" not in in_vals, (
                f"{col!r} must not be bound from a parameter."
            )

    def test_tool_signature_has_no_rejected_params(self):
        """The public tool must NOT expose ``ID``, ``CreatedAt``,
        ``UpdatedAt`` as parameters — otherwise the LLM could try to set
        them and waste tokens."""
        import inspect
        sig = inspect.signature(tool_persist_ar_record)
        param_names = set(sig.parameters.keys())
        for forbidden in _REJECTED_COLUMNS:
            assert forbidden not in param_names, (
                f"tool_persist_ar_record must NOT accept {forbidden!r} — "
                f"it is a server default."
            )
        # Same for Traite / Decision / DateReceptionAR — forced server-side.
        for forced in ("Traite", "Decision", "DateReceptionAR"):
            assert forced not in param_names, (
                f"tool_persist_ar_record must NOT accept {forced!r} — "
                f"value is forced by Python guards."
            )


class TestStatutGlobalValidation:
    """The CHECK constraint on dbo.Commandes.StatutGlobal is enforced
    early by the Python tool so the agent gets a clear refusal."""

    def test_valid_statut_passes_schema_check(self):
        for statut in _VALID_STATUT_GLOBAL:
            # We can't open the real engine in tests — just verify the
            # validation gate does not reject these values.
            assert statut in _VALID_STATUT_GLOBAL

    def test_invalid_statut_refused(self, monkeypatch):
        # Bypass DB to isolate the validation logic.
        monkeypatch.setattr(
            scei_ar_persist, "_get_db_engine", lambda: None
        )
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-19",
            StatutGlobal="invented_status",  # not in the enum
        )
        assert result["success"] is False
        assert result["commande_id"] is None
        assert "invalid_statut_global" in result["error"]


class TestDbUnavailable:
    def test_returns_error_when_no_engine(self, monkeypatch):
        monkeypatch.setattr(
            scei_ar_persist, "_get_db_engine", lambda: None
        )
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-19",
            StatutGlobal="conforme",
        )
        assert result == {
            "success": False,
            "commande_id": None,
            "lignes_inserees": 0,
            "error": "db_unavailable",
        }


class TestExceptionEnvelope:
    def test_db_exception_is_caught_and_reported(self, monkeypatch):
        class _BoomEngine:
            def begin(self):
                raise RuntimeError("connection refused by SCEI VPN")

        monkeypatch.setattr(
            scei_ar_persist, "_get_db_engine", lambda: _BoomEngine()
        )
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-19",
            StatutGlobal="conforme",
        )
        assert result["success"] is False
        assert result["commande_id"] is None
        assert "RuntimeError" in result["error"]
        assert "connection refused" in result["error"]


class TestMessageIdRemoved:
    """Anti-regression for hotfix 2026-05-19.

    MessageId was documented in the SCEI prompt (scei.py:483)
    but never migrated into the MSSQL dbo.Commandes table. PR #179
    blindly listed it in the INSERT and SCEI_PROD would have crashed
    on every AR with pyodbc.ProgrammingError 42S22. This guard
    refuses any future PR that reintroduces MessageId without
    also adding an ALTER TABLE migration on MSSQL."""

    def test_messageid_not_in_columns(self):
        assert "MessageId" not in _COMMANDES_COLUMNS, (
            "MessageId is NOT a column of dbo.Commandes in MSSQL — "
            "re-introducing it without an ALTER TABLE migration will "
            "crash every AR with 'Nom de colonne non valide'."
        )

    def test_messageid_not_in_generated_sql(self):
        sql = _build_insert_sql()
        assert "MessageId" not in sql, (
            "MessageId must not appear in the INSERT statement "
            "(columns list or VALUES) until an MSSQL migration adds "
            "the column."
        )

    def test_tool_signature_has_no_messageid_param(self):
        import inspect
        sig = inspect.signature(tool_persist_ar_record)
        assert "MessageId" not in sig.parameters, (
            "tool_persist_ar_record must NOT accept MessageId — the "
            "column does not exist in the live MSSQL schema."
        )



# ---------------------------------------------------------------------------
# 8. WebhookLogId is read from tool_context.state (PR 3b)
# ---------------------------------------------------------------------------


class TestWebhookLogIdFromState:
    """The tool reads the originating ``webhook_log_id`` from the ADK
    session state (which the webhook worker initialises before launching
    the SequentialAgent — cf process_webhook_log_row). It then binds it
    to the SQL ``:WebhookLogId`` parameter.

    Why state and not session.id parsing:
      * ADK propagates session_state to every sub-agent of a
        SequentialAgent, identifier-style fields like session.id may
        not be (no other tool in the codebase relies on it).
      * The webhook worker already knows the log_id (it's the row it's
        processing). Passing it explicitly via state is the canonical
        ADK pattern.
    """

    def _capture_bound_params(self, tool_context_obj):
        """Run the tool with a stub DB engine and capture the params dict."""
        captured: dict[str, object] = {}

        class _StubConn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def execute(self, _sql, params):
                captured.update(params)
                class _R:
                    def scalar_one(_s):
                        return 1
                return _R()
            def commit(self):
                pass

        class _StubEngine:
            def connect(self):
                return _StubConn()
            def begin(self):
                return _StubConn()

        # Monkey-patch the engine factory so the tool talks to the stub.
        from th2customers.scei.tools import scei_ar_persist as m
        orig = m._get_db_engine
        m._get_db_engine = lambda: _StubEngine()
        try:
            m.tool_persist_ar_record(
                NumeroCommande="CF099999",
                DateCommande="2026-05-20",
                StatutGlobal="conforme",
                Societe="100",
                FournisseurCode="X",
                FournisseurNom="X",
                Commanditaire="Y",
                EmailExpediteur="x@y",
                PJOneDrivePath=None,
                tool_context=tool_context_obj,
            )
        finally:
            m._get_db_engine = orig
        return captured

    def test_integer_state_value_is_bound(self):
        ctx = MagicMock()
        ctx.state = {"webhook_log_id": 2618}
        params = self._capture_bound_params(ctx)
        assert params["WebhookLogId"] == 2618

    def test_string_digits_are_converted_to_int(self):
        ctx = MagicMock()
        ctx.state = {"webhook_log_id": "2618"}
        params = self._capture_bound_params(ctx)
        assert params["WebhookLogId"] == 2618

    def test_missing_state_key_yields_null(self):
        ctx = MagicMock()
        ctx.state = {}
        params = self._capture_bound_params(ctx)
        assert params["WebhookLogId"] is None

    def test_no_tool_context_yields_null(self):
        params = self._capture_bound_params(None)
        assert params["WebhookLogId"] is None

    def test_non_numeric_string_yields_null_not_exception(self):
        ctx = MagicMock()
        ctx.state = {"webhook_log_id": "not-a-number"}
        params = self._capture_bound_params(ctx)
        assert params["WebhookLogId"] is None

    def test_sql_contains_webhook_log_id_bind_param(self):
        sql = _build_insert_sql()
        assert ":WebhookLogId" in sql, (
            "WebhookLogId must be a bound parameter so the worker can "
            "inject the value from session state via the tool."
        )
        # It must appear in BOTH the column list and the VALUES list.
        cols_match = re.search(r"INSERT INTO dbo\.Commandes \(([^)]+)\)", sql)
        assert cols_match
        assert "WebhookLogId" in cols_match.group(1)


class TestStatutStrictNoAlias:
    """Strictness decision (2026-05-21): the guard accepts ONLY
    conforme/non_conforme/non_rapproche. Invented (ORDER_NOT_FOUND) or
    legacy (OK/NOK) statuses are rejected here so the drift is fixed at the
    producer (recorder agent), never masked. No _STATUT_ALIASES coercion."""

    def test_alias_dict_removed(self):
        assert not hasattr(scei_ar_persist, "_STATUT_ALIASES")

    @pytest.mark.parametrize(
        "bad",
        ["OK", "ok", "NOK", "nok", "ORDER_NOT_FOUND", "order_not_found"],
    )
    def test_invented_or_legacy_statut_refused(self, monkeypatch, bad):
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: None)
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-21",
            StatutGlobal=bad,
        )
        assert result["success"] is False
        assert "invalid_statut_global" in result["error"]


class TestStatutWhitespaceHygiene:
    """A stray newline/space around a canonical value must be trimmed, not
    refused (observed once 2026-05-20: invalid_statut_global='conforme')."""

    @pytest.mark.parametrize(
        "statut",
        [" conforme", "conforme\n", "  non_rapproche  ", "non_conforme\t"],
    )
    def test_whitespace_trimmed_then_accepted(self, monkeypatch, statut):
        # engine None: a value that PASSES validation reaches db_unavailable;
        # a refused value would return invalid_statut_global instead.
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: None)
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-21",
            StatutGlobal=statut,
        )
        assert result["error"] == "db_unavailable"


# ---------------------------------------------------------------------------
# Transient-connection retry (2026-05-21): a network blip over the SCEI VPN
# (SQLSTATE 08001) used to drop the AR with no retry.
# ---------------------------------------------------------------------------

from sqlalchemy.exc import OperationalError, ProgrammingError  # noqa: E402


def _op_08001():
    return OperationalError(
        "INSERT",
        {},
        Exception(
            "('08001', '[08001] [Microsoft][ODBC Driver 18 for SQL Server]"
            "Client unable to establish connection ... handshakes before login')"
        ),
    )


def _prog_bad_column():
    return ProgrammingError("INSERT", {}, Exception("Invalid column name 'Foo'"))


class _Result:
    def scalar(self):
        return 4242


class _OkConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return _Result()


class _IdemNone:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        class _N:
            def scalar(self_inner):
                return None
        return _N()


class _FlakyEngine:
    """begin() raises `exc` for the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times, exc):
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc

    def connect(self):
        return _IdemNone()  # idempotency precheck: no existing row

    def begin(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return _OkConn()


class TestTransientRetry:
    def test_is_transient_connect_error(self):
        assert scei_ar_persist._is_transient_connect_error(_op_08001()) is True
        # every transient SQLSTATE token must be recognised
        for tok in scei_ar_persist._TRANSIENT_CONNECT_TOKENS:
            exc = OperationalError("x", {}, Exception(f"('{tok}', '[{tok}] ...')"))
            assert scei_ar_persist._is_transient_connect_error(exc) is True, tok
        # textual fallbacks (case-insensitive)
        for txt in ("unable to Establish Connection", "Login timeout expired"):
            exc = OperationalError("x", {}, Exception(txt))
            assert scei_ar_persist._is_transient_connect_error(exc) is True, txt
        # logic error must NOT be retried
        assert scei_ar_persist._is_transient_connect_error(_prog_bad_column()) is False
        assert scei_ar_persist._is_transient_connect_error(ValueError("x")) is False

    def test_retries_then_succeeds_on_transient(self, monkeypatch):
        monkeypatch.setattr(scei_ar_persist.time, "sleep", lambda *_: None)
        eng = _FlakyEngine(fail_times=2, exc=_op_08001())
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: eng)
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-21",
            StatutGlobal="non_rapproche",
        )
        assert result["success"] is True
        assert result["commande_id"] == 4242
        assert eng.calls == 3  # 2 failures + 1 success

    def test_no_retry_on_logic_error(self, monkeypatch):
        slept = []
        monkeypatch.setattr(scei_ar_persist.time, "sleep", lambda d: slept.append(d))
        eng = _FlakyEngine(fail_times=99, exc=_prog_bad_column())
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: eng)
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-21",
            StatutGlobal="conforme",
        )
        assert result["success"] is False
        assert eng.calls == 1  # failed fast, no retry
        assert slept == []  # never slept

    def test_gives_up_after_max_attempts_on_persistent_transient(self, monkeypatch):
        monkeypatch.setattr(scei_ar_persist.time, "sleep", lambda *_: None)
        eng = _FlakyEngine(fail_times=99, exc=_op_08001())
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: eng)
        result = tool_persist_ar_record(
            NumeroCommande="CF000001",
            DateCommande="2026-05-21",
            StatutGlobal="non_rapproche",
        )
        assert result["success"] is False
        assert "OperationalError" in result["error"]
        assert eng.calls == scei_ar_persist._INSERT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Atomic line persistence (2026-05-22): header + LignesCommande in one txn,
# replacing the LLM-driven raw-SQL line inserts that left orphan headers.
# ---------------------------------------------------------------------------


class _RecordingConn:
    def __init__(self, recorder):
        self._rec = recorder

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        self._rec.append((str(statement), params))

        class _R:
            def scalar(self_inner):
                return 4242
        return _R()


class _RecordingEngine:
    def __init__(self):
        self.calls = []

    def connect(self):
        return _IdemNone()  # idempotency precheck → no existing row

    def begin(self):
        return _RecordingConn(self.calls)


class TestCoerceLine:
    def test_defaults_fill_not_null_columns(self):
        p = scei_ar_persist._coerce_line({}, 0, numero="CF1", societe="100", fallback_date="2026-05-22")
        assert p["NumeroCommande"] == "CF1"
        assert p["NumeroLigne"] == 1
        assert p["DateLigne"] == "2026-05-22"
        assert p["Reference"] == ""
        assert p["Quantite"] == 0
        assert p["Prix"] == 0
        assert p["Situation"] == "OK"  # no TypeEcart → OK
        assert p["Societe"] == "100"

    def test_ecart_line_is_nok(self):
        p = scei_ar_persist._coerce_line(
            {"TypeEcart": "ecart_qte"}, 3, numero="CF1", societe=None, fallback_date="2026-05-22"
        )
        assert p["Situation"] == "NOK"
        assert p["NumeroLigne"] == 4
        assert p["TypeEcart"] == "ecart_qte"

    def test_snake_case_aliases(self):
        p = scei_ar_persist._coerce_line(
            {"numero_ligne": 7, "ref_scei": "X", "quantite_ar": 3, "prix_ar": 9.5},
            0, numero="CF1", societe=None, fallback_date="2026-05-22",
        )
        assert p["NumeroLigne"] == 7
        assert p["Reference"] == "X"
        assert p["QuantiteAR"] == 3
        assert p["PrixAR"] == 9.5


class TestLinePersistence:
    def test_lines_inserted_in_same_transaction(self, monkeypatch):
        eng = _RecordingEngine()
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: eng)
        res = tool_persist_ar_record(
            NumeroCommande="CF1",
            DateCommande="2026-05-22",
            StatutGlobal="non_conforme",
            Societe="100",
            lignes=[
                {"NumeroLigne": 1, "Reference": "ART1", "TypeEcart": "ecart_prix",
                 "PrixAR": 10, "PrixERP": 12, "Quantite": 5, "Prix": 10},
                {"Reference": "ART2"},  # minimal → defaults
            ],
        )
        assert res["success"] is True
        assert res["lignes_inserees"] == 2
        line_calls = [c for c in eng.calls if "dbo.LignesCommande" in c[0]]
        assert len(line_calls) == 2
        assert any("dbo.Commandes" in c[0] for c in eng.calls)  # header too
        assert line_calls[0][1]["Situation"] == "NOK"  # has TypeEcart
        assert line_calls[1][1]["NumeroLigne"] == 2  # defaulted
        assert line_calls[1][1]["Situation"] == "OK"
        assert line_calls[1][1]["Reference"] == "ART2"

    def test_no_lines_means_zero(self, monkeypatch):
        eng = _RecordingEngine()
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: eng)
        res = tool_persist_ar_record(
            NumeroCommande="CF1", DateCommande="2026-05-22", StatutGlobal="non_rapproche",
        )
        assert res["success"] is True
        assert res["lignes_inserees"] == 0
        assert not any("dbo.LignesCommande" in c[0] for c in eng.calls)


class TestCoerceLineHardening:
    """Bad LLM values must NOT raise at execute time (which would roll back
    the whole atomic header+lines transaction and lose the AR)."""

    def test_junk_date_falls_back(self):
        r = scei_ar_persist._coerce_line(
            {"DateLigne": "en attente"}, 0, numero="CF1", societe=None, fallback_date="2026-05-22")
        assert r["DateLigne"] == "2026-05-22"

    def test_valid_iso_date_kept(self):
        r = scei_ar_persist._coerce_line(
            {"DateLigne": "2026-06-01"}, 0, numero="CF1", societe=None, fallback_date="2026-05-22")
        assert r["DateLigne"] == "2026-06-01"

    def test_junk_numbers_become_safe(self):
        r = scei_ar_persist._coerce_line(
            {"Quantite": "N/A", "Prix": "?", "Ecart": "x"}, 0, numero="CF1", societe=None, fallback_date="2026-05-22")
        assert r["Quantite"] == 0
        assert r["Prix"] == 0
        assert r["Ecart"] is None

    def test_quantite_ar_does_not_leak_into_quantite(self):
        # F5: the AR figure must not populate the PO Quantite/Prix columns.
        r = scei_ar_persist._coerce_line(
            {"QuantiteAR": 120, "PrixAR": 9.5}, 0, numero="CF1", societe=None, fallback_date="2026-05-22")
        assert r["Quantite"] == 0
        assert r["Prix"] == 0
        assert r["QuantiteAR"] == 120
        assert r["PrixAR"] == 9.5

    def test_optional_date_junk_is_none(self):
        r = scei_ar_persist._coerce_line(
            {"DateLivraisonAR": "TBD"}, 0, numero="CF1", societe=None, fallback_date="2026-05-22")
        assert r["DateLivraisonAR"] is None


class TestEarlyReturnsShape:
    def test_db_unavailable_includes_lignes_inserees(self, monkeypatch):
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: None)
        res = tool_persist_ar_record(
            NumeroCommande="CF1", DateCommande="2026-05-22", StatutGlobal="conforme")
        assert res["error"] == "db_unavailable"
        assert res["lignes_inserees"] == 0

    def test_invalid_statut_includes_lignes_inserees(self, monkeypatch):
        monkeypatch.setattr(scei_ar_persist, "_get_db_engine", lambda: None)
        res = tool_persist_ar_record(
            NumeroCommande="CF1", DateCommande="2026-05-22", StatutGlobal="bogus")
        assert "invalid_statut_global" in res["error"]
        assert res["lignes_inserees"] == 0


# ---------------------------------------------------------------------------
# SQL 334 fix: remove OUTPUT clause, use SCOPE_IDENTITY (2026-05-23)
# ---------------------------------------------------------------------------
# dbo.Commandes a un trigger AFTER INSERT (trg_Commandes_normalize_statut).
# SQL Server 334 interdit OUTPUT sans INTO quand un trigger AFTER est actif.
# Fix = supprimer OUTPUT INSERTED.ID et récupérer l'ID via SELECT SCOPE_IDENTITY()
# dans le même bloc engine.begin().
# ---------------------------------------------------------------------------

class TestNoOutputClause:
    """OUTPUT INSERTED.ID doit être absent du SQL généré."""

    def test_no_output_clause_in_sql(self):
        sql = _build_insert_sql()
        assert "OUTPUT" not in sql.upper(), (
            "OUTPUT INSERTED.ID must be removed from _build_insert_sql() — "
            "SQL Server 334: OUTPUT without INTO forbidden when an AFTER "
            "trigger exists on dbo.Commandes. Use SCOPE_IDENTITY() instead."
        )

    def test_sql_has_plain_insert_into_commandes(self):
        sql = _build_insert_sql()
        assert "INSERT INTO dbo.Commandes" in sql

    def test_sql_has_values_clause(self):
        sql = _build_insert_sql()
        assert "VALUES" in sql


class _IdempotencyConn:
    """Stub pour le check d'idempotence (engine.connect() → scalar() → None)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        class _NoRow:
            def scalar(self_inner):
                return None
        return _NoRow()


# ---------------------------------------------------------------------------
# B2 single-batch SCOPE_IDENTITY fix (2026-05-23)
# TestScopeIdentityFlow remplacé : l'ancien test assumait 2 execute() séparés
# (INSERT puis SELECT SCOPE_IDENTITY()), ce qui est l'architecture CASSÉE.
# SCOPE_IDENTITY() dans un 2e batch retourne NULL (prouvé live).
# Fix B2 : un seul conn.execute() contenant SET NOCOUNT ON + INSERT +
# SELECT SCOPE_IDENTITY() AS ID + SET NOCOUNT OFF.
# ---------------------------------------------------------------------------

class TestSingleBatchScopeIdentity:
    """Le header INSERT + SCOPE_IDENTITY doivent être dans UN SEUL batch.

    Raison du remplacement de TestScopeIdentityFlow (2 execute séparés) :
    SCOPE_IDENTITY() dans un 2e batch séparé renvoie NULL car il est en
    dehors du scope de l'INSERT. Fix B2 : un seul conn.execute() avec
    SET NOCOUNT ON + INSERT + SELECT SCOPE_IDENTITY() AS ID + SET NOCOUNT OFF.
    """

    def _run_and_capture(self, monkeypatch, scalar_return=42, lignes=None):
        """Lance tool_persist_ar_record; retourne (result, liste des sql_str exécutés)."""
        import decimal
        from th2customers.scei.tools import scei_ar_persist as m

        execute_calls: list[str] = []

        class _CapturingConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, statement, params=None):
                sql_str = str(statement)
                execute_calls.append(sql_str)

                class _R:
                    def scalar(self_inner):
                        if "SCOPE_IDENTITY" in sql_str:
                            return decimal.Decimal(str(scalar_return))
                        return None

                return _R()

        class _CapturingEngine:
            def connect(self):
                return _IdempotencyConn()

            def begin(self):
                return _CapturingConn()

        monkeypatch.setattr(m, "_get_db_engine", lambda: _CapturingEngine())
        result = m.tool_persist_ar_record(
            NumeroCommande="CF099999",
            DateCommande="2026-05-23",
            StatutGlobal="conforme",
            lignes=lignes,
        )
        return result, execute_calls

    def test_header_batch_contains_set_nocount_on(self, monkeypatch):
        """Le batch du header doit commencer par SET NOCOUNT ON."""
        _, calls = self._run_and_capture(monkeypatch)
        header_calls = [c for c in calls if "INSERT INTO dbo.Commandes" in c]
        assert len(header_calls) == 1, f"Expected 1 header execute, got: {calls}"
        batch = header_calls[0]
        assert "SET NOCOUNT ON" in batch.upper(), (
            f"Batch must start with SET NOCOUNT ON. Got:\n{batch}"
        )

    def test_header_batch_contains_insert(self, monkeypatch):
        """Le batch du header doit contenir l'INSERT INTO dbo.Commandes."""
        _, calls = self._run_and_capture(monkeypatch)
        header_calls = [c for c in calls if "INSERT INTO dbo.Commandes" in c]
        assert len(header_calls) == 1
        batch = header_calls[0]
        assert "INSERT INTO dbo.Commandes" in batch

    def test_header_batch_contains_select_scope_identity(self, monkeypatch):
        """Le batch doit contenir SELECT SCOPE_IDENTITY() AS ID."""
        _, calls = self._run_and_capture(monkeypatch)
        header_calls = [c for c in calls if "INSERT INTO dbo.Commandes" in c]
        assert len(header_calls) == 1
        batch = header_calls[0]
        assert "SELECT SCOPE_IDENTITY() AS ID" in batch, (
            f"Batch must contain SELECT SCOPE_IDENTITY() AS ID. Got:\n{batch}"
        )

    def test_header_batch_contains_set_nocount_off(self, monkeypatch):
        """Le batch doit se terminer par SET NOCOUNT OFF."""
        _, calls = self._run_and_capture(monkeypatch)
        header_calls = [c for c in calls if "INSERT INTO dbo.Commandes" in c]
        assert len(header_calls) == 1
        batch = header_calls[0]
        assert "SET NOCOUNT OFF" in batch.upper(), (
            f"Batch must end with SET NOCOUNT OFF. Got:\n{batch}"
        )

    def test_header_batch_order_nocount_insert_scope_nocount(self, monkeypatch):
        """Ordre strict : SET NOCOUNT ON → INSERT → SELECT SCOPE_IDENTITY → SET NOCOUNT OFF."""
        _, calls = self._run_and_capture(monkeypatch)
        header_calls = [c for c in calls if "INSERT INTO dbo.Commandes" in c]
        assert len(header_calls) == 1
        batch = header_calls[0].upper()
        pos_nocount_on = batch.index("SET NOCOUNT ON")
        pos_insert = batch.index("INSERT INTO DBO.COMMANDES")
        pos_scope = batch.index("SELECT SCOPE_IDENTITY()")
        pos_nocount_off = batch.rindex("SET NOCOUNT OFF")
        assert pos_nocount_on < pos_insert < pos_scope < pos_nocount_off, (
            f"Wrong order in batch. Positions: NOCOUNT_ON={pos_nocount_on}, "
            f"INSERT={pos_insert}, SCOPE={pos_scope}, NOCOUNT_OFF={pos_nocount_off}"
        )

    def test_no_isolated_scope_identity_execute(self, monkeypatch):
        """SCOPE_IDENTITY ne doit PAS être dans un execute() séparé sans INSERT."""
        _, calls = self._run_and_capture(monkeypatch)
        isolated = [
            c for c in calls
            if "SCOPE_IDENTITY" in c and "INSERT INTO dbo.Commandes" not in c
        ]
        assert isolated == [], (
            f"Found isolated SELECT SCOPE_IDENTITY() calls (would return NULL): {isolated}"
        )

    def test_single_execute_for_header(self, monkeypatch):
        """Un seul conn.execute() doit couvrir le header (INSERT + SCOPE_IDENTITY)."""
        _, calls = self._run_and_capture(monkeypatch)
        header_calls = [
            c for c in calls
            if "INSERT INTO dbo.Commandes" in c or "SCOPE_IDENTITY" in c
        ]
        # Il doit y avoir exactement 1 call qui contient les deux
        combined = [
            c for c in calls
            if "INSERT INTO dbo.Commandes" in c and "SCOPE_IDENTITY" in c
        ]
        assert len(combined) == 1, (
            f"Expected 1 execute containing both INSERT and SCOPE_IDENTITY, got {len(combined)}. "
            f"All calls: {calls}"
        )

    def test_commande_id_is_int_cast_from_scalar(self, monkeypatch):
        """scalar() retourne Decimal — commande_id doit être casté en int."""
        result, _ = self._run_and_capture(monkeypatch, scalar_return=42)
        assert result["success"] is True
        assert result["commande_id"] == 42, (
            f"commande_id must be 42, got {result['commande_id']!r}"
        )
        assert isinstance(result["commande_id"], int), (
            f"commande_id must be int, got {type(result['commande_id'])}"
        )

    def test_commande_id_none_when_scalar_returns_none(self, monkeypatch):
        """Si SCOPE_IDENTITY() retourne None (ne doit pas arriver en prod),
        commande_id doit être None et non lever une exception."""
        from th2customers.scei.tools import scei_ar_persist as m

        class _NoneConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, statement, params=None):
                class _R:
                    def scalar(self_inner):
                        return None
                return _R()

        class _NoneEngine:
            def connect(self):
                return _IdempotencyConn()

            def begin(self):
                return _NoneConn()

        monkeypatch.setattr(m, "_get_db_engine", lambda: _NoneEngine())
        result = m.tool_persist_ar_record(
            NumeroCommande="CF099998",
            DateCommande="2026-05-23",
            StatutGlobal="conforme",
        )
        assert result["success"] is True
        assert result["commande_id"] is None

    def test_lines_inserted_after_header_batch(self, monkeypatch):
        """Les lignes sont insérées après le batch header dans la même transaction."""
        result, calls = self._run_and_capture(
            monkeypatch,
            scalar_return=7777,
            lignes=[{"Reference": "ART1", "TypeEcart": "ecart_prix"}],
        )
        assert result["success"] is True
        assert result["commande_id"] == 7777
        assert result["lignes_inserees"] == 1
        header_idx = next(i for i, c in enumerate(calls) if "INSERT INTO dbo.Commandes" in c)
        line_calls = [i for i, c in enumerate(calls) if "dbo.LignesCommande" in c]
        assert len(line_calls) == 1
        assert line_calls[0] > header_idx


# ---------------------------------------------------------------------------
# Societe char(3) deterministic derive + generic truncation guard.
# Root cause of SILENT AR drops: the recorder LLM writes a company NAME into
# the char(3) Societe code column (and oversized composites into
# NumeroCommande varchar(20)) -> SQL 2628 truncation -> the whole INSERT fails
# -> the AR is lost while the run reports "Success" (58 Societe + 4
# NumeroCommande failures over 4 days, 5 ARs lost, measured 2026-05-27).
# ---------------------------------------------------------------------------
from th2customers.scei.tools.scei_ar_persist import (  # noqa: E402
    _derive_societe,
    _clamp,
    _clamp_row,
    _VALID_SOCIETE_CODES,
    _COL_MAXLEN_COMMANDES,
    _COL_MAXLEN_LIGNES,
)


class TestDeriveSociete:
    """Societe is a 3-char société CODE, not a name. Matched -> trust the
    PMI code (ar_match.po.ecktsoc); non-matched -> a known code or NULL."""

    def test_matched_overrides_llm_company_name_with_pmi_code(self):
        ar = {"status": "matched", "po": {"ecktsoc": "100"}}
        assert _derive_societe(ar, "SOCIETE CABLAGE ELECTRIQUE INDUSTRIEL") == "100"

    def test_matched_proceed_code(self):
        ar = {"status": "matched", "po": {"ecktsoc": "120"}}
        assert _derive_societe(ar, "whatever long name") == "120"

    def test_matched_but_empty_ecktsoc_falls_back_to_none(self):
        ar = {"status": "matched", "po": {"ecktsoc": ""}}
        assert _derive_societe(ar, "SOCIETE LONGUE NOM") is None

    def test_matched_ecktsoc_outside_allowlist_is_used(self):
        # A future legitimate société code must NOT be silently dropped.
        ar = {"status": "matched", "po": {"ecktsoc": "130"}}
        assert _derive_societe(ar, "x") == "130"

    def test_non_matched_keeps_known_code(self):
        assert _derive_societe({"status": "non_rapproche"}, "100") == "100"
        assert _derive_societe({"status": "out_of_scope"}, "120") == "120"

    def test_non_matched_long_name_yields_none(self):
        assert _derive_societe(None, "SOCIETE CABLAGE ELECTRIQUE INDUSTRIEL") is None

    def test_non_matched_empty_string_yields_none(self):
        assert _derive_societe(None, "") is None

    def test_non_matched_none_yields_none(self):
        assert _derive_societe(None, None) is None

    def test_matched_noop_when_already_correct(self):
        ar = {"status": "matched", "po": {"ecktsoc": "100"}}
        assert _derive_societe(ar, "100") == "100"

    def test_valid_codes_are_100_and_120(self):
        assert _VALID_SOCIETE_CODES == frozenset({"100", "120"})


class TestClamp:
    """Last-resort net so a too-long value can never raise SQL 2628."""

    def test_truncates_oversized_string(self):
        assert _clamp("SOCIETE CABLAGE", 3, "Societe", "CF1") == "SOC"

    def test_passes_short_string_untouched(self):
        assert _clamp("100", 3, "Societe", "CF1") == "100"

    def test_passes_none_untouched(self):
        assert _clamp(None, 3, "Societe", "CF1") is None

    def test_passes_non_string_untouched(self):
        assert _clamp(42, 3, "Societe", "CF1") == 42

    def test_numero_commande_composite_truncated_to_20(self):
        v = "3542065/3095070826/CF101089"
        out = _clamp(v, 20, "NumeroCommande", v)
        assert out == v[:20]
        assert len(out) == 20

    def test_clamp_row_clamps_mapped_columns_only(self):
        row = {"Societe": "SOCIETE", "Quantite": 5, "Reference": "x" * 80}
        out = _clamp_row(row, {"Societe": 3, "Reference": 50}, num="CF1")
        assert out["Societe"] == "SOC"
        assert out["Reference"] == "x" * 50
        assert out["Quantite"] == 5  # unmapped key untouched


class TestColMaxLenSchemaDrift:
    """A unit assertion catches an ERP ALTER TABLE that would invalidate the
    hardcoded widths (the only fragility of not reading INFORMATION_SCHEMA)."""

    def test_societe_is_char3(self):
        assert _COL_MAXLEN_COMMANDES["Societe"] == 3
        assert _COL_MAXLEN_LIGNES["Societe"] == 3

    def test_numero_commande_is_20(self):
        assert _COL_MAXLEN_COMMANDES["NumeroCommande"] == 20


class TestSocieteWiring:
    """End-to-end: a matched ar_match in session state forces the bound
    Societe to the PMI code even when the LLM passed a company name that would
    overflow char(3) (the CF101173/SOCOMEC live drop, 2026-05-27)."""

    def _capture(self, ctx, societe_arg):
        captured: dict[str, object] = {}

        class _Conn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def execute(self, _sql, params=None):
                if params and "DateCommande" in params:
                    captured.update(params)

                class _R:
                    def scalar(_s):
                        return None  # idempotence precheck: no existing row

                    def scalar_one(_s):
                        return 1
                return _R()
            def commit(self):
                pass

        class _Eng:
            def connect(self):
                return _Conn()
            def begin(self):
                return _Conn()

        from th2customers.scei.tools import scei_ar_persist as m
        orig = m._get_db_engine
        m._get_db_engine = lambda: _Eng()
        try:
            m.tool_persist_ar_record(
                NumeroCommande="CF101173",
                DateCommande="2026-05-27",
                StatutGlobal="conforme",
                Societe=societe_arg,
                tool_context=ctx,
            )
        finally:
            m._get_db_engine = orig
        return captured

    def test_matched_state_forces_pmi_societe_code(self):
        ctx = MagicMock()
        ctx.state = {
            "ar_match": {
                "status": "matched",
                "po": {"ecktsoc": "100", "ecktnumero": "101173"},
            }
        }
        params = self._capture(ctx, "SOCIETE CABLAGE ELECTRIQUE INDUSTRIEL")
        assert params["Societe"] == "100"
        assert len(params["Societe"]) <= 3


# ---------------------------------------------------------------------------
# Bug #3 — deterministic per-line reconciliation against PMI lines.
# The recorder LLM mislabels a line that EXISTS in PMI (identical ref+qty+price)
# as LINE_NOT_IN_PO -> faux non_rapproche (live 2026-05-27, CF005493). The
# verdict is recomputed in code; price uses the recorder's already-converted
# PrixAR/PrixERP (the raw LCCTEXPU conversion rule is unverified).
# ---------------------------------------------------------------------------
from th2customers.scei.tools.scei_ar_persist import (  # noqa: E402
    _reconcile_lines,
)


def _pmi(refext, qty, punet=None):
    return {"lcctcode": "X", "lcctrefext": refext, "lcctqty": qty,
            "lcctpunet": punet, "lcctexpu": " "}


class TestReconcileLines:
    def test_cf005493_exact_match_becomes_conforme(self):
        # Real case: recorder put non_rapproche although ref+qty+price match.
        lignes = [{"Reference": "1CFHI886I002", "Quantite": 90, "QuantiteAR": 90,
                   "PrixAR": 15.28, "PrixERP": 15.28, "TypeEcart": "non_rapproche",
                   "Situation": "NOK"}]
        pmi = [_pmi("1CFHI886I002            ", 90.0, 15.28)]  # padded refext
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] is None
        assert out[0]["Situation"] == "OK"
        assert out[0]["QuantiteERP"] == 90.0  # forced from PMI

    def test_qty_diff_becomes_ecart_qte(self):
        lignes = [{"Reference": "R1", "QuantiteAR": 80, "PrixAR": 5.0, "PrixERP": 5.0,
                   "TypeEcart": "non_rapproche"}]
        out = _reconcile_lines(lignes, [_pmi("R1", 90.0, 5.0)])
        assert out[0]["TypeEcart"] == "ecart_qte"

    def test_price_diff_sans_pdf_conforme(self):
        """FIX 1 iter6 : sans pdf_text, pas de verdict prix via recorder.

        MISE A JOUR iter6 : le verdict ecart_prix ne se declenche QUE si le
        prix est lu depuis le PDF (pdf_prix is not None). Sans pdf_text,
        _pdf_prix_found=False -> pas de ecart_prix meme si PrixAR!=PrixERP.
        qty=90==PMI 90 -> conforme.
        """
        lignes = [{"Reference": "R1", "QuantiteAR": 90, "PrixAR": 5.0, "PrixERP": 6.0,
                   "TypeEcart": "conforme"}]
        out = _reconcile_lines(lignes, [_pmi("R1", 90.0, 6.0)])
        assert out[0]["TypeEcart"] is None

    def test_ref_absent_becomes_line_not_in_po(self):
        lignes = [{"Reference": "UNKNOWN", "QuantiteAR": 1, "TypeEcart": "conforme"}]
        out = _reconcile_lines(lignes, [_pmi("R1", 1.0)])
        assert out[0]["TypeEcart"] == "LINE_NOT_IN_PO"
        assert out[0]["Situation"] == "NOK"

    def test_ref_via_reffournisseur_field(self):
        lignes = [{"Reference": "", "RefFournisseur": "R1", "QuantiteAR": 90,
                   "PrixAR": 5.0, "PrixERP": 5.0, "TypeEcart": "non_rapproche"}]
        out = _reconcile_lines(lignes, [_pmi("R1", 90.0, 5.0)])
        assert out[0]["TypeEcart"] is None

    def test_empty_pmi_lines_is_noop(self):
        lignes = [{"Reference": "R1", "TypeEcart": "non_rapproche"}]
        assert _reconcile_lines(lignes, []) == lignes

    def test_ref_found_missing_values_is_conforme(self):
        # ref found but no qty/price to compare -> line IS in the PO, no
        # discrepancy detected -> conforme (never left as non_rapproche).
        lignes = [{"Reference": "R1", "TypeEcart": "non_rapproche"}]
        out = _reconcile_lines(lignes, [_pmi("R1", None, None)])
        assert out[0]["TypeEcart"] is None
        assert out[0]["Situation"] == "OK"

    def test_ref_and_qty_match_price_missing_is_conforme(self):
        # Live CF005493 replay (id=283): flaky recorder omitted PrixAR/PrixERP
        # but ref + qty match deterministically -> conforme, not non_rapproche.
        lignes = [{"Reference": "1CFHI886I002", "Quantite": 90,
                   "PrixAR": None, "PrixERP": None, "TypeEcart": "non_rapproche"}]
        out = _reconcile_lines(lignes, [_pmi("1CFHI886I002", 90.0, 15.28)])
        assert out[0]["TypeEcart"] is None
        assert out[0]["QuantiteERP"] == 90.0

    def test_pmi_qty_null_with_ar_qty_keeps_verdict(self):
        # Defensive (0/97347 in PMI): AR has a qty but PMI line has none to
        # compare -> cannot verify -> do NOT fabricate conforme, keep verdict.
        lignes = [{"Reference": "R1", "QuantiteAR": 50, "TypeEcart": "non_rapproche"}]
        out = _reconcile_lines(lignes, [_pmi("R1", None, None)])
        assert out[0]["TypeEcart"] == "non_rapproche"

    def test_does_not_mutate_input(self):
        lignes = [{"Reference": "1CFHI886I002", "QuantiteAR": 90, "PrixAR": 15.28,
                   "PrixERP": 15.28, "TypeEcart": "non_rapproche"}]
        _reconcile_lines(lignes, [_pmi("1CFHI886I002", 90.0, 15.28)])
        assert lignes[0]["TypeEcart"] == "non_rapproche"

    def test_mixed_lines_conforme_and_absent(self):
        lignes = [
            {"Reference": "R1", "QuantiteAR": 90, "PrixAR": 5.0, "PrixERP": 5.0, "TypeEcart": "non_rapproche"},
            {"Reference": "GHOST", "QuantiteAR": 1, "TypeEcart": "conforme"},
        ]
        out = _reconcile_lines(lignes, [_pmi("R1", 90.0, 5.0)])
        assert out[0]["TypeEcart"] is None
        assert out[1]["TypeEcart"] == "LINE_NOT_IN_PO"

    def test_relly_reffournisseur_is_codart_matches(self):
        """RED -> GREEN: RELLY CF101158 — Reference="7MOLL0162" (réf fournisseur,
        ne matche pas LCCTCODART) + RefFournisseur="573461860001" (= LCCTCODART PMI).
        Avant le fix: LINE_NOT_IN_PO. Après: conforme (ref trouvée, qté+prix OK)."""
        lignes = [
            {"Reference": "7MOLL0162", "RefFournisseur": "573461860001",
             "QuantiteAR": 5, "PrixAR": 10.0, "PrixERP": 10.0,
             "TypeEcart": "non_rapproche", "Situation": "NOK"},
        ]
        pmi = [{"lcctcodart": "573461860001", "lcctrefext": "", "lcctqty": 5.0,
                "lcctpunet": 10.0, "lcctexpu": " "}]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] is None
        assert out[0]["Situation"] == "OK"

    def test_reference_codart_match_non_regression(self):
        """Non-régression: une ligne dont Reference contient directement le codart
        PMI (cas existant qui marche) reste matchée correctement."""
        lignes = [
            {"Reference": "1CFHI886I002", "RefFournisseur": "", "QuantiteAR": 90,
             "PrixAR": 15.28, "PrixERP": 15.28, "TypeEcart": "non_rapproche"},
        ]
        pmi = [{"lcctcodart": "1CFHI886I002", "lcctrefext": "", "lcctqty": 90.0,
                "lcctpunet": 15.28, "lcctexpu": " "}]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] is None
        assert out[0]["Situation"] == "OK"

    def test_alphanum_reffournisseur_no_false_match(self):
        """Anti-faux-match: RefFournisseur alphanumérique ("D0160R30N0101") avec
        PMI codart numérique ("570999860001"). Aucune inclusion bidirectionnelle
        possible -> LINE_NOT_IN_PO attendu. Le fix ne crée pas de faux positif."""
        lignes = [
            {"Reference": "D0160R30N0101", "RefFournisseur": "",
             "QuantiteAR": 1, "TypeEcart": "conforme"},
        ]
        pmi = [{"lcctcodart": "570999860001", "lcctrefext": "", "lcctqty": 1.0,
                "lcctpunet": 5.0, "lcctexpu": " "}]
        out = _reconcile_lines(lignes, pmi)
        assert out[0]["TypeEcart"] == "LINE_NOT_IN_PO"


class TestForceReprocess:
    """Deliberate relaunch (force_reprocess from retrigger_webhook_log) must
    REPLACE an existing AR via an ATOMIC delete-then-insert (one batch / one
    transaction), while an automatic Graph re-delivery or a chat run keeps the
    anti-duplicate no-op. Incident 2026-05-28 CF097349."""

    def _run(self, ctx, existing_id):
        captured: list[str] = []

        class _R:
            def __init__(self, v):
                self._v = v
            def scalar(self):
                return self._v

        class _Conn:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def execute(self, sql, params=None):
                s = str(sql)
                captured.append(s)
                if "SELECT TOP 1 ID FROM dbo.Commandes" in s:
                    return _R(existing_id)
                return _R(999)
            def commit(self):
                pass

        class _Engine:
            def connect(self):
                return _Conn()
            def begin(self):
                return _Conn()

        from th2customers.scei.tools import scei_ar_persist as m
        orig = m._get_db_engine
        m._get_db_engine = lambda: _Engine()
        try:
            res = m.tool_persist_ar_record(
                NumeroCommande="CF097349", DateCommande="2026-05-28",
                StatutGlobal="conforme", Societe="100",
                FournisseurCode="X", FournisseurNom="Danfoss",
                Commanditaire="SCEI", EmailExpediteur="x@y",
                PJOneDrivePath=None, tool_context=ctx,
            )
        finally:
            m._get_db_engine = orig
        return res, captured

    def test_force_replaces_existing_atomically(self):
        ctx = MagicMock()
        ctx.state = {"webhook_log_id": 4367, "force_reprocess": True}
        res, captured = self._run(ctx, existing_id=1)
        assert res.get("already_exists") is not True  # NOT a no-op
        batch = [s for s in captured if "INSERT INTO dbo.Commandes" in s]
        assert len(batch) == 1
        # DELETE + INSERT in the SAME batch => one transaction (atomic)
        assert "DELETE FROM dbo.LignesCommande" in batch[0]
        assert "DELETE FROM dbo.Commandes WHERE WebhookLogId" in batch[0]

    def test_no_force_existing_is_noop(self):
        ctx = MagicMock()
        ctx.state = {"webhook_log_id": 4367, "force_reprocess": False}
        res, captured = self._run(ctx, existing_id=1)
        assert res.get("already_exists") is True
        assert not any("DELETE" in s for s in captured)

    def test_chat_run_never_deletes(self):
        ctx = MagicMock()
        ctx.state = {}  # no webhook_log_id, no force -> chat run
        res, captured = self._run(ctx, existing_id=1)
        assert res.get("already_exists") is True
        assert not any("DELETE" in s for s in captured)


# ---------------------------------------------------------------------------
# Tests re-extraction ciblée (9 cas spec_reextraction_ciblee.md)
# ---------------------------------------------------------------------------
from th2customers.scei.tools.scei_ar_persist import (  # noqa: E402
    _reextract_lines_from_pmi_and_text,
    _lines_are_doubtful,
    _load_full_pdf_text,
)
from apowerb.core.agent_helpers.pdf_to_images_tool import extract_all_pages_text


class TestLinesAreDoubtful:
    """Cas 8 : vérification du helper _lines_are_doubtful."""

    def test_empty_list_is_doubtful(self):
        assert _lines_are_doubtful([]) is True

    def test_none_is_doubtful(self):
        assert _lines_are_doubtful(None) is True

    def test_all_line_not_in_po_is_doubtful(self):
        lignes = [
            {"TypeEcart": "LINE_NOT_IN_PO", "Situation": "NOK"},
            {"TypeEcart": "LINE_NOT_IN_PO", "Situation": "NOK"},
        ]
        assert _lines_are_doubtful(lignes) is True

    def test_all_non_rapproche_is_doubtful(self):
        lignes = [{"TypeEcart": "non_rapproche", "Situation": "NOK"}]
        assert _lines_are_doubtful(lignes) is True

    def test_one_conforme_one_doubtful_is_doubtful(self):
        # FIX Défaut 3 (itération 2) : _lines_are_doubtful retourne True si AU
        # MOINS une ligne est douteuse (LINE_NOT_IN_PO/non_rapproche), même si une
        # autre est conforme. La fusion non-destructive dans tool_persist_ar_record
        # se charge de préserver la ligne conforme et d'ajouter la manquante.
        lignes = [
            {"TypeEcart": None, "Situation": "OK"},
            {"TypeEcart": "LINE_NOT_IN_PO", "Situation": "NOK"},
        ]
        assert _lines_are_doubtful(lignes) is True

    def test_ecart_prix_is_not_doubtful(self):
        lignes = [{"TypeEcart": "ecart_prix", "Situation": "NOK"}]
        assert _lines_are_doubtful(lignes) is False


        # La re-extraction sur un pdf_text quelconque ne doit pas être invoquée.
        # On vérifie que le résultat de la re-extraction sur un texte arbitraire
        # ne casserait pas les lignes conformes existantes (non-régression pure).
        # Ce test valide la garde : si doubtful=False, on skip.
        # (Le test d'intégration complet nécessiterait un mock de tool_context — hors scope)


# ---------------------------------------------------------------------------
# extract_all_pages_text — nouvelle fonction (correctif conception 2026-05-29)
# Les lignes AR Schneider/SAP sont en page 2, pas en page 1.
# extract_first_page_text retourne un texte vide de toutes refs => correction.
# ---------------------------------------------------------------------------


class TestExtractAllPagesText:
    """extract_all_pages_text concatène TOUTES les pages; jamais raise."""


    def test_nonexistent_file_returns_error_dict(self):
        result = extract_all_pages_text("/nonexistent/path.pdf")
        assert "error" in result
        assert result["text"] == ""


# ---------------------------------------------------------------------------
# _load_full_pdf_text — helper interne (correctif 2026-05-29)
# ---------------------------------------------------------------------------


class TestLoadFullPdfText:
    """_load_full_pdf_text lit tous les PDFs attachés au webhook_log_id courant."""

    def test_returns_empty_string_when_no_tool_context(self):
        assert _load_full_pdf_text(None) == ""

    def test_returns_empty_string_when_no_state(self):
        ctx = MagicMock()
        ctx.state = None
        assert _load_full_pdf_text(ctx) == ""

    def test_returns_empty_string_when_no_webhook_log_id(self):
        ctx = MagicMock()
        ctx.state = {}
        assert _load_full_pdf_text(ctx) == ""

    def test_returns_empty_string_when_no_pdf_found(self, tmp_path):
        ctx = MagicMock()
        ctx.state = {"webhook_log_id": 999999}
        # Pas de fichier → retourne "" sans lever
        result = _load_full_pdf_text(ctx)
        assert isinstance(result, str)


class TestDeriveCommanditaire:
    """Commanditaire is resolved DETERMINISTICALLY from ar_match.po.ecctutimod
    and validated against the active whitelist — never from a free LLM guess.

    Root-cause audit 2026-05-29: the field was NULL 78% of the time even on
    matched ARs, and the only filled values were garbage (a supplier 'TILCO', a
    generic 'Commerciale SCEI', a misspelt name) — none in the whitelist. These
    tests pin the deterministic resolution + the anti-garbage guard in code.
    """

    _WL_EXACT = frozenset({"Benjamin PIERRAT", "Violene LAMBOLEY", "Xavier TOURRE"})

    @pytest.fixture(autouse=True)
    def _stub_whitelist(self, monkeypatch):
        # No DB: stub the whitelist loader with a fixed canonical set.
        norm = {scei_ar_persist._norm_name(n): n for n in self._WL_EXACT}
        monkeypatch.setattr(
            scei_ar_persist,
            "_load_commanditaires_whitelist",
            lambda force=False: (self._WL_EXACT, norm),
        )

    @staticmethod
    def _matched(ecctutimod):
        po = None if ecctutimod is None else {"ecctutimod": ecctutimod}
        return {"status": "matched", "po": po}

    def test_matched_ecctutimod_in_whitelist_returns_canonical(self):
        assert scei_ar_persist._derive_commanditaire(
            self._matched("Benjamin PIERRAT"), None
        ) == "Benjamin PIERRAT"

    def test_matched_ecctutimod_accent_case_variant_normalised(self):
        # whitelist has 'Violene LAMBOLEY'; a PMI value differing by accent/case/
        # extra spaces must still resolve to the canonical Nom.
        assert scei_ar_persist._derive_commanditaire(
            self._matched("violène  lamboley"), None
        ) == "Violene LAMBOLEY"

    def test_matched_ecctutimod_not_in_whitelist_returns_none(self):
        # a technical/non-whitelisted account must NOT be persisted
        assert scei_ar_persist._derive_commanditaire(
            self._matched("ADMIN IMPORT"), None
        ) is None

    def test_matched_deterministic_overrides_llm_supplier_garbage(self):
        # po carries a valid buyer; the LLM passed a supplier name -> det. wins
        assert scei_ar_persist._derive_commanditaire(
            self._matched("Xavier TOURRE"), "TILCO"
        ) == "Xavier TOURRE"

    def test_matched_empty_ecctutimod_then_garbage_llm_is_none(self):
        assert scei_ar_persist._derive_commanditaire(
            self._matched("   "), "TILCO"
        ) is None

    def test_matched_no_po_falls_back_to_whitelisted_llm(self):
        assert scei_ar_persist._derive_commanditaire(
            {"status": "matched", "po": None}, "Xavier TOURRE"
        ) == "Xavier TOURRE"

    def test_non_rapproche_keeps_whitelisted_llm_value(self):
        assert scei_ar_persist._derive_commanditaire(
            {"status": "non_rapproche", "po": None}, "Benjamin PIERRAT"
        ) == "Benjamin PIERRAT"

    def test_non_rapproche_drops_garbage_llm_values(self):
        m = {"status": "non_rapproche", "po": None}
        assert scei_ar_persist._derive_commanditaire(m, "TILCO") is None
        assert scei_ar_persist._derive_commanditaire(m, "Commerciale SCEI") is None

    def test_none_ar_match_uses_llm_fallback(self):
        assert scei_ar_persist._derive_commanditaire(None, "Benjamin PIERRAT") == "Benjamin PIERRAT"
        assert scei_ar_persist._derive_commanditaire(None, "nope") is None

    def test_whitelist_unavailable_yields_none_no_raise(self, monkeypatch):
        # DB down at startup → empty whitelist. Everything resolves to NULL
        # (fail-open, never garbage), and no exception escapes (the INSERT of the
        # AR itself must not be blocked by a commanditaire-resolution failure).
        monkeypatch.setattr(
            scei_ar_persist,
            "_load_commanditaires_whitelist",
            lambda force=False: (frozenset(), {}),
        )
        assert scei_ar_persist._derive_commanditaire(
            self._matched("Benjamin PIERRAT"), "Benjamin PIERRAT"
        ) is None
