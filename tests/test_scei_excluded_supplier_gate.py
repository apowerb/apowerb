"""TDD tests for the deterministic 'fournisseurs exclus' gate.

Tests are WRITTEN FIRST (RED phase). All DB calls are mocked.
No real DB connection is made. Never call pytest tests/ (shared DB).

Covers:
- _sender_matches_excluded() pure matching logic
- build_excluded_supplier_gate_callback() ADK callback
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_rows(*abbreviations: str) -> list[dict]:
    """Build fake FournisseursExclus rows."""
    return [
        {
            "CLKTCODE": str(i + 1),
            "CLCTABRE": abbr,
            "Raison": f"Exclu: {abbr}",
        }
        for i, abbr in enumerate(abbreviations)
    ]


# ---------------------------------------------------------------------------
# Phase 1 — _sender_matches_excluded (PURE function, NO mock)
# ---------------------------------------------------------------------------


class TestSenderMatchesExcluded:
    """Pure unit tests — zero I/O."""

    def setup_method(self):
        from th2customers.scei.gates import _sender_matches_excluded
        self._fn = _sender_matches_excluded

    # --- token >= 6 chars: substring match on full sender_str ---

    def test_trane_domain_long_token_excluded(self):
        """TRANE (5 chars < 6) → domaine only. Domain tranetechnologies.com contient TRANE."""
        rows = _make_rows("TRANE")
        matched, token = self._fn(
            "Lucia Contractor <lucia@contractor.tranetechnologies.com>", rows
        )
        assert matched is True
        assert token == "TRANE"

    def test_long_token_substring_full_match(self):
        """PROCEED (>=6) → substring full sender_str."""
        rows = _make_rows("PROCEED")
        matched, token = self._fn(
            "Proceed Logistics <info@proceed-logistics.de>", rows
        )
        assert matched is True
        assert token == "PROCEED"

    def test_long_token_in_display_name(self):
        """SIEMENS (>=6) → must match display name 'Siemens'."""
        rows = _make_rows("SIEMENS")
        matched, token = self._fn(
            "Siemens AG <noreply@siemens-global.com>", rows
        )
        assert matched is True
        assert token == "SIEMENS"

    def test_unknown_supplier_not_excluded(self):
        """Supplier not in exclusion list → not matched."""
        rows = _make_rows("TRANE", "PROCEED")
        matched, token = self._fn(
            "Pascal Martin <p@acme.fr>", rows
        )
        assert matched is False
        assert token is None

    # --- token < 6 chars: domain-only matching (anti-false-positive) ---

    def test_scei_short_token_no_false_positive_display_name(self):
        """SCEI (4) → domain only. display name 'SCEI Service' ne doit pas exclu
        si le domaine n'est pas scei. Prouve la protection display name."""
        rows = _make_rows("SCEI")
        # Display name contains SCEI, but domain is 'autre.fr'
        matched, token = self._fn(
            "SCEI Service <x@autre.fr>", rows
        )
        assert matched is False
        assert token is None

    def test_cael_short_token_macaelerie_false_positive(self):
        """CAS LIMITE DOCUMENTE: CAEL (4) → domain only.
        Le domaine 'macaelerie.fr' CONTIENT 'cael' -> exclu À TORT.
        Ce faux positif est ATTENDU et documenté ci-dessous.
        ACTION: valider avec David si besoin d'une exclusion par liste blanche
        ou d'une règle de longueur minimale >= 5 pour les tokens courts.
        Pour l'instant on prouve ce comportement (c'est le comportement réel).
        """
        rows = _make_rows("CAEL")
        # Domain macaelerie.fr contains 'cael' → false exclude
        matched, token = self._fn(
            "Pascal <p@macaelerie.fr>", rows
        )
        # CAEL (4) → domain-only match → macaelerie.fr contient cael → EXCLU
        # Ce cas EST un faux positif — documenter ici, décision David requise.
        assert matched is True  # faux positif attendu/documenté
        assert token == "CAEL"

    def test_scei_short_token_domain_contains_scei(self):
        """SCEI (4) → domain match si le domaine contient 'scei'."""
        rows = _make_rows("SCEI")
        matched, token = self._fn(
            "Fournisseur X <contact@scei-fournisseur.com>", rows
        )
        assert matched is True
        assert token == "SCEI"

    def test_empty_token_ignored(self):
        """CLCTABRE vide → ignoré. Pas de match fantôme."""
        rows = _make_rows("")
        matched, token = self._fn(
            "Anyone <a@anywhere.com>", rows
        )
        assert matched is False
        assert token is None

    def test_whitespace_only_token_ignored(self):
        """Token avec espaces seulement → ignoré."""
        rows = [{"CLKTCODE": "1", "CLCTABRE": "   ", "Raison": "?"}]
        matched, token = self._fn(
            "Anyone <a@anywhere.com>", rows
        )
        assert matched is False
        assert token is None

    def test_empty_sender_str_returns_false(self):
        """Expéditeur vide → (False, None) sans crash."""
        rows = _make_rows("TRANE")
        matched, token = self._fn("", rows)
        assert matched is False
        assert token is None

    def test_first_match_returned(self):
        """Plusieurs tokens pourraient matcher → le premier de la liste est renvoyé."""
        rows = _make_rows("SIEMENS", "NOREPLY")
        matched, token = self._fn(
            "Siemens <noreply@siemens-global.com>", rows
        )
        assert matched is True
        assert token == "SIEMENS"

    def test_empty_rows_list(self):
        """Aucune exclusion en DB → rien exclu."""
        matched, token = self._fn(
            "Anyone <a@anywhere.com>", []
        )
        assert matched is False
        assert token is None

    def test_case_insensitive_long_token(self):
        """Matching est case-insensitive."""
        rows = _make_rows("SIEMENS")
        matched, token = self._fn(
            "siemens ag <info@siemens.de>", rows
        )
        assert matched is True
        assert token == "SIEMENS"


# ---------------------------------------------------------------------------
# Phase 2 — build_excluded_supplier_gate_callback (mocked DB + state)
# ---------------------------------------------------------------------------


def _make_callback_context(state: dict):
    """Minimal ADK CallbackContext mock."""
    ctx = MagicMock()
    ctx.state = state
    return ctx


def _make_run_sql_excluded(rows: list[dict]):
    """Mock run_sql renvoyant les rows données (simule FournisseursExclus)."""
    def run_sql(sql: str) -> dict:
        return {
            "success": True,
            "sql": sql,
            "row_count": len(rows),
            "data": rows,
        }
    run_sql.__name__ = "tool_run_sql_suiviar"
    return run_sql


class TestExcludedSupplierGateCallback:
    """Gate callback — tous les appels DB sont mockés."""

    def _build_gate(self, rows: list[dict], fail_db: bool = False):
        """Build the gate callback with mocked DB infrastructure."""
        from th2customers.scei.gates import build_excluded_supplier_gate_callback

        def mock_load_tool_config_params(tool_config_id, owner_id):
            return ("tool_run_sql_suiviar", {"host": "mock", "db": "mock"})

        def mock_make_database_tools(agent_name, db_params):
            if fail_db:
                raise RuntimeError("DB connection failed")
            tool = _make_run_sql_excluded(rows)
            tool.__name__ = "tool_run_sql"
            return [tool]

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            mock_load_tool_config_params,
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            mock_make_database_tools,
        ):
            return build_excluded_supplier_gate_callback(
                output_key="ar_intake",
                tool_config_id="17",
                owner_id="test_owner",
            )

    def test_excluded_supplier_returns_content_and_state(self):
        """Expéditeur exclu → Content renvoyé + state[ar_intake] = not_ar JSON valide."""
        from google.genai.types import Content

        rows = _make_rows("TRANE")
        gate = self._build_gate(rows)
        state: dict = {
            "email_sender": "Lucia <lucia@contractor.tranetechnologies.com>"
        }
        ctx = _make_callback_context(state)

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql_suiviar", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [_make_run_sql_excluded(rows)],
        ):
            result = gate(ctx)

        assert isinstance(result, Content), "Doit retourner Content pour court-circuiter ADK"
        assert "ar_intake" in state, "state[ar_intake] doit être écrit"
        payload = json.loads(state["ar_intake"])
        assert payload["email_classification"] == "not_ar"
        assert "fournisseur exclu" in payload["raison"].lower()
        assert payload["extraction_results"] == {}

    def test_non_excluded_supplier_returns_none(self):
        """Expéditeur non exclu → None (intake tourne)."""
        rows = _make_rows("TRANE")
        gate = self._build_gate(rows)
        state: dict = {"email_sender": "Pascal <p@acme.fr>"}
        ctx = _make_callback_context(state)

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql_suiviar", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [_make_run_sql_excluded(rows)],
        ):
            result = gate(ctx)

        assert result is None

    def test_db_fail_returns_none_and_warns(self, caplog):
        """Échec DB → fail-open: None renvoyé + WARNING loggé."""
        import logging

        rows: list[dict] = []

        def mock_make_db_fail(**kw):
            raise RuntimeError("Connection timeout")

        gate_fn = None

        from th2customers.scei.gates import build_excluded_supplier_gate_callback

        def mock_load(tool_config_id, owner_id):
            return ("tool_run_sql_suiviar", {"host": "mock"})

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            mock_load,
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            mock_make_db_fail,
        ):
            gate_fn = build_excluded_supplier_gate_callback("ar_intake", "17", "owner")

        state: dict = {"email_sender": "Lucia <lucia@trane.com>"}
        ctx = _make_callback_context(state)

        with caplog.at_level(logging.WARNING):
            with patch(
                "th2customers.scei.gates.load_tool_config_params",
                mock_load,
            ), patch(
                "th2customers.scei.gates.make_database_tools",
                mock_make_db_fail,
            ):
                result = gate_fn(ctx)

        assert result is None, "Fail-open: doit retourner None en cas d'erreur DB"
        assert "ar_intake" not in state, "Ne doit pas écrire state en cas d'erreur"
        assert any("WARNING" in r.levelname or r.levelno >= logging.WARNING
                   for r in caplog.records), "Doit logguer un WARNING"

    def test_sender_absent_returns_none(self):
        """Pas d'email_sender dans state → None (gate skip propre)."""
        rows = _make_rows("TRANE")
        state: dict = {}  # pas de email_sender
        ctx = _make_callback_context(state)

        from th2customers.scei.gates import build_excluded_supplier_gate_callback

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql_suiviar", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [_make_run_sql_excluded(rows)],
        ):
            gate = build_excluded_supplier_gate_callback("ar_intake", "17", "owner")
            result = gate(ctx)

        assert result is None

    def test_excluded_payload_is_valid_scei_intake_payload(self):
        """Le payload écrit en state doit être valide selon SCEIIntakePayload."""
        from th2customers.scei.schemas import SCEIIntakePayload

        rows = _make_rows("TRANE")
        state: dict = {
            "email_sender": "Lucia <lucia@tranetechnologies.com>"
        }
        ctx = _make_callback_context(state)

        from th2customers.scei.gates import build_excluded_supplier_gate_callback

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql_suiviar", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [_make_run_sql_excluded(rows)],
        ):
            gate = build_excluded_supplier_gate_callback("ar_intake", "17", "owner")
            gate(ctx)

        raw = state.get("ar_intake")
        assert raw is not None
        parsed = json.loads(raw)
        # Must validate against the schema (raises if invalid)
        payload = SCEIIntakePayload.model_validate(parsed)
        assert payload.email_classification == "not_ar"


# ---------------------------------------------------------------------------
# Phase 3 — maybe_wire_excluded_supplier_gate + composition
# ---------------------------------------------------------------------------


class TestMaybeWireExcludedSupplierGate:
    """Wiring guard + composition avec attachment_pdf_gate."""

    def test_wires_when_flag_set(self):
        """Flag excluded_supplier_gate=True + tool_config17 → gate câblé."""
        from th2customers.scei.gates import maybe_wire_excluded_supplier_gate

        agent_details = {
            "output_schema_name": "SCEIIntakePayload",
            "excluded_supplier_gate": True,
            "agent_tools": ["tool_config17"],
            "owner_id": "test_owner",
        }
        kwargs: dict = {}

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [],
        ):
            wired = maybe_wire_excluded_supplier_gate(
                agent_details, "ar_intake", kwargs
            )

        assert wired is True
        assert "before_agent_callback" in kwargs

    def test_wires_via_output_schema_without_flag(self):
        """Fallback prod : output_schema_name == SCEIIntakePayload + tool_config17,
        SANS le flag (qui n'est pas une colonne DB) → gate câblé quand même.
        C'est ce qui rend le gate actif en prod après resync."""
        from th2customers.scei.gates import maybe_wire_excluded_supplier_gate

        agent_details = {
            "output_schema_name": "SCEIIntakePayload",
            # pas de "excluded_supplier_gate" : reproduit l'agent reel en DB
            "agent_tools": ["tool_config17"],
            "owner_id": "test_owner",
        }
        kwargs: dict = {}
        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [],
        ):
            wired = maybe_wire_excluded_supplier_gate(
                agent_details, "ar_intake", kwargs
            )
        assert wired is True
        assert "before_agent_callback" in kwargs

    def test_not_wired_without_schema_or_flag(self):
        """Ni output_schema_name SCEIIntakePayload ni flag → gate non câblé."""
        from th2customers.scei.gates import maybe_wire_excluded_supplier_gate

        agent_details = {
            "output_schema_name": "ChatPayload",
            "agent_tools": ["tool_config17"],
            "owner_id": "test_owner",
        }
        kwargs: dict = {}
        wired = maybe_wire_excluded_supplier_gate(
            agent_details, "ar_intake", kwargs
        )
        assert wired is False

    def test_composition_with_attachment_pdf_gate(self):
        """Le gate exclu + attachment_pdf_gate → before_agent_callback = liste [exclu, pdf]."""
        from th2customers.scei.gates import maybe_wire_attachment_pdf_gate, maybe_wire_excluded_supplier_gate

        agent_details = {
            "output_schema_name": "SCEIIntakePayload",
            "attachment_pdf_gate": True,
            "excluded_supplier_gate": True,
            "agent_tools": ["tool_config17"],
            "owner_id": "test_owner",
        }
        kwargs: dict = {}

        # Step 1: wire attachment pdf gate (pose un scalaire)
        maybe_wire_attachment_pdf_gate(agent_details, "ar_intake", kwargs)
        assert callable(kwargs.get("before_agent_callback")), "PDF gate = scalaire"

        # Step 2: wire excluded supplier gate (doit composer en liste)
        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            lambda *a: ("tool_run_sql", {"host": "mock"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            lambda **kw: [],
        ):
            wired = maybe_wire_excluded_supplier_gate(
                agent_details, "ar_intake", kwargs
            )

        assert wired is True
        cbs = kwargs["before_agent_callback"]
        assert isinstance(cbs, list), "Composition = liste"
        assert len(cbs) == 2, "2 callbacks: exclu + pdf"
        # L'exclu est EN PREMIER
        assert "excluded" in cbs[0].__name__.lower() or "exclu" in cbs[0].__name__.lower(), (
            f"Le gate exclu doit être en premier. Got: {cbs[0].__name__}"
        )

    def test_no_tool_config_not_wired(self):
        """Pas de tool_config dans agent_tools → non câblé."""
        from th2customers.scei.gates import maybe_wire_excluded_supplier_gate

        agent_details = {
            "output_schema_name": "SCEIIntakePayload",
            "excluded_supplier_gate": True,
            "agent_tools": [],
            "owner_id": "test_owner",
        }
        kwargs: dict = {}
        wired = maybe_wire_excluded_supplier_gate(
            agent_details, "ar_intake", kwargs
        )
        assert wired is False
