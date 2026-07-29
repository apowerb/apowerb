"""TDD tests for deterministic PMI matching gate.

Tests are WRITTEN FIRST (RED phase). All DB calls are mocked.
No real DB connection is made.

Covers:
- match_ar_to_pmi() pure logic
- build_pmi_match_gate_callback() ADK callback
- maybe_wire_pmi_match_gate() wiring guard
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_run_sql(success: bool = True, data: list | None = None, error: str = ""):
    """Build a mock run_sql callable mirroring real tool_run_sql return format."""

    def run_sql(sql: str) -> dict:
        if not success:
            return {"success": False, "sql": sql, "error": error or "db error"}
        return {
            "success": True,
            "sql": sql,
            "row_count": len(data or []),
            "data": data or [],
        }

    return run_sql


# CF101090 fixtures (validated by critic3 against real PMI schema)
_HEADER_CF101090 = {
    "ECKTSOC": "SC ",
    "ECKTNUMERO": "101090",
    "ECKTINDICE": "A  ",
    "ECCTCODE": "TILCO ",
    "ECCTNOM": "TILCO FRANCE SAS                        ",
    "ECCTREFCDE": "821667-SF           ",
    "ECCTUTIMOD": "Benjamin PIERRAT                        ",
}

_LINE_CF101090 = {
    "LCKTLIGNE": "001",
    "LCCTCODE": "TILCO ",
    "LCCTREFEXT": "821667-SF               ",
    "LCCNQTECDE": "30.000",
    "LCCNPUNET": "123.450",
    "LCCTEXPU": " ",
}


def _make_pmi_run_sql_cf101090():
    """Returns a run_sql mock: 1) header query -> 1 row, 2) lines query -> 1 row."""
    call_count = [0]

    def run_sql(sql: str) -> dict:
        call_count[0] += 1
        sql_upper = sql.strip().upper()
        if "ECOMFOU" in sql_upper and "LCOMFOU" not in sql_upper:
            return {"success": True, "sql": sql, "row_count": 1, "data": [_HEADER_CF101090]}
        if "LCOMFOU" in sql_upper and "LCKTSOC" in sql_upper:
            return {"success": True, "sql": sql, "row_count": 1, "data": [_LINE_CF101090]}
        return {"success": True, "sql": sql, "row_count": 0, "data": []}

    run_sql._call_count = call_count
    run_sql.__name__ = "tool_run_sql"
    return run_sql


# ---------------------------------------------------------------------------
# Tests for match_ar_to_pmi()
# ---------------------------------------------------------------------------


class TestMatchArToPmi:
    """Unit tests for the pure matching logic."""

    def test_cf101090_matched_correct_po_and_line(self):
        """(a) CF101090 -> status=matched, po fields correct, line has float qty+price."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [{"ligne_numero": 1, "ref_fournisseur": "821667-SF", "qty": 30, "unit_price_ht": 123.45}],
            },
        }
        run_sql = _make_pmi_run_sql_cf101090()
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result["po"] is not None
        assert result["po"]["ecctcode"] == "TILCO"
        assert result["po"]["ecctnom"] == "TILCO FRANCE SAS"
        assert result["po"]["ecktsoc"] == "SC"
        assert result["po"]["ecktnumero"] == "101090"
        # ECCTUTIMOD (buyer) must be surfaced + trimmed, so the recorder's
        # deterministic _derive_commanditaire can resolve it (2026-05-29).
        assert result["po"]["ecctutimod"] == "Benjamin PIERRAT"

        assert len(result["lines"]) == 1
        line = result["lines"][0]
        assert line["lcctrefext"] == "821667-SF"
        assert line["lcctqty"] == 30.0
        assert isinstance(line["lcctqty"], float)
        assert abs(line["lcctpunet"] - 123.45) < 0.01
        assert isinstance(line["lcctpunet"], float)
        assert line["lcctexpu"] == ""

    def test_header_select_includes_ecctutimod(self):
        """The PMI header SELECT(s) must request ECCTUTIMOD — else po cannot
        carry the buyer and Commanditaire silently stays NULL (2026-05-29)."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        seen_sql: list[str] = []

        def run_sql(sql: str) -> dict:
            seen_sql.append(sql)
            if "ECOMFOU" in sql.upper() and "LCOMFOU" not in sql.upper():
                return {"success": True, "sql": sql, "row_count": 1, "data": [_HEADER_CF101090]}
            return {"success": True, "sql": sql, "row_count": 0, "data": []}

        intake = {
            "email_classification": "ar",
            "extraction_results": {"commande_number_sql": "101090", "lines": []},
        }
        match_ar_to_pmi(intake, run_sql)
        header_selects = [
            s for s in seen_sql
            if "ECOMFOU" in s.upper() and "LCOMFOU" not in s.upper()
        ]
        assert header_selects, "no header SELECT was issued"
        assert all("ECCTUTIMOD" in s.upper() for s in header_selects)

    def test_tilco_invalid_number_out_of_scope(self):
        """(b) commande_number_sql='TILCO' does not match regex d{6} -> out_of_scope."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "TILCO",
                "commande_number_display": "TILCO",
                "lines": [],
            },
        }
        run_sql = _make_run_sql()
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "out_of_scope"
        assert result["po"] is None
        assert "TILCO" in result["diagnostic"]

    def test_empty_number_out_of_scope(self):
        """(b2) empty commande_number_sql -> out_of_scope."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "",
                "commande_number_display": "",
                "lines": [],
            },
        }
        run_sql = _make_run_sql()
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "out_of_scope"
        assert result["po"] is None

    def test_no_header_found_non_rapproche(self):
        """(c) run_sql returns empty -> non_rapproche (fallbacks B1/B2 also empty)."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "999999",
                "commande_number_display": "CF999999",
                "lines": [],
            },
        }
        run_sql = _make_run_sql(success=True, data=[])
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "non_rapproche"
        assert result["po"] is None

    def test_sql_failure_raises_exception(self):
        """(d) run_sql success=False -> exception raised, NOT silent non_rapproche."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        }
        run_sql = _make_run_sql(success=False, error="MSSQL 08001: cannot connect")

        with pytest.raises(Exception, match="MSSQL 08001"):
            match_ar_to_pmi(intake, run_sql)

    def test_more_than_50_lines_truncated(self):
        """(e) >50 lines -> truncated to 50, diagnostic mentions truncation."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        header_data = [{
            "ECKTSOC": "SC",
            "ECKTNUMERO": "101090",
            "ECKTINDICE": "A",
            "ECCTCODE": "SUPP",
            "ECCTNOM": "Supplier Corp",
            "ECCTREFCDE": "REF001",
        }]
        lines_data = [
            {
                "LCKTLIGNE": str(i).zfill(3),
                "LCCTCODE": "SUPP",
                "LCCTREFEXT": f"REF{i:06d}",
                "LCCNQTECDE": "10.000",
                "LCCNPUNET": "5.000",
                "LCCTEXPU": " ",
            }
            for i in range(1, 55)
        ]

        def run_sql(sql: str) -> dict:
            sql_upper = sql.strip().upper()
            if "ECOMFOU" in sql_upper and "LCOMFOU" not in sql_upper:
                return {"success": True, "sql": sql, "row_count": 1, "data": header_data}
            if "LCOMFOU" in sql_upper:
                return {"success": True, "sql": sql, "row_count": len(lines_data), "data": lines_data}
            return {"success": True, "sql": sql, "row_count": 0, "data": []}

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        }
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert len(result["lines"]) == 50
        assert "50" in result["diagnostic"] or "tronqu" in result["diagnostic"].lower()

    def test_nchar_space_padding_stripped(self):
        """(f) nchar values with trailing spaces are stripped."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        padded_header = {
            "ECKTSOC": "SC         ",
            "ECKTNUMERO": "101090     ",
            "ECKTINDICE": "A          ",
            "ECCTCODE": "SUPP       ",
            "ECCTNOM": "Supplier                               ",
            "ECCTREFCDE": "REF001              ",
        }
        padded_line = {
            "LCKTLIGNE": "001   ",
            "LCCTCODE": "SUPP      ",
            "LCCTREFEXT": "REF001                  ",
            "LCCNQTECDE": "5.000",
            "LCCNPUNET": "10.000",
            "LCCTEXPU": " ",
        }

        def run_sql(sql: str) -> dict:
            sql_upper = sql.strip().upper()
            if "ECOMFOU" in sql_upper and "LCOMFOU" not in sql_upper:
                return {"success": True, "sql": sql, "row_count": 1, "data": [padded_header]}
            if "LCOMFOU" in sql_upper:
                return {"success": True, "sql": sql, "row_count": 1, "data": [padded_line]}
            return {"success": True, "sql": sql, "row_count": 0, "data": []}

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        }
        result = match_ar_to_pmi(intake, run_sql)

        assert result["po"]["ecktsoc"] == "SC"
        assert result["po"]["ecctcode"] == "SUPP"
        assert result["po"]["ecctnom"] == "Supplier"
        assert result["lines"][0]["lcctrefext"] == "REF001"
        assert result["lines"][0]["lcctexpu"] == ""

    def test_decimal_string_to_float(self):
        """(g) _serialize_row turns Decimal->str; match_ar_to_pmi casts them to float."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        header = {
            "ECKTSOC": "SC",
            "ECKTNUMERO": "101090",
            "ECKTINDICE": "A",
            "ECCTCODE": "SUPP",
            "ECCTNOM": "Supplier",
            "ECCTREFCDE": "REF",
        }
        line = {
            "LCKTLIGNE": "001",
            "LCCTCODE": "SUPP",
            "LCCTREFEXT": "REF001",
            "LCCNQTECDE": "30.000",
            "LCCNPUNET": "123.450",
            "LCCTEXPU": "C",
        }

        def run_sql(sql: str) -> dict:
            sql_upper = sql.strip().upper()
            if "ECOMFOU" in sql_upper and "LCOMFOU" not in sql_upper:
                return {"success": True, "sql": sql, "row_count": 1, "data": [header]}
            if "LCOMFOU" in sql_upper:
                return {"success": True, "sql": sql, "row_count": 1, "data": [line]}
            return {"success": True, "sql": sql, "row_count": 0, "data": []}

        intake = {
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        }
        result = match_ar_to_pmi(intake, run_sql)

        assert result["lines"][0]["lcctqty"] == 30.0
        assert abs(result["lines"][0]["lcctpunet"] - 123.45) < 0.001
        assert isinstance(result["lines"][0]["lcctqty"], float)
        assert result["lines"][0]["lcctexpu"] == "C"


# ---------------------------------------------------------------------------
# Tests for build_pmi_match_gate_callback()
# ---------------------------------------------------------------------------


def _make_fake_context(state: dict, agent_name: str = "scei_ar_matcher"):
    """Minimal fake ADK CallbackContext with state dict access."""
    ctx = MagicMock()
    ctx.state = state
    ctx.agent_name = agent_name
    return ctx


class TestBuildPmiMatchGateCallback:

    def test_not_ar_intake_returns_out_of_scope_content(self):
        """(a) ar_intake.email_classification==not_ar -> Content with out_of_scope payload."""
        from th2customers.scei.gates import build_pmi_match_gate_callback

        intake_payload = json.dumps({"email_classification": "not_ar", "raison": "no_pdf", "extraction_results": {}})
        state = {"ar_intake": intake_payload}
        ctx = _make_fake_context(state)

        class FakePart:
            def __init__(self, text): self.text = text
        class FakeContent:
            def __init__(self, role, parts):
                self.role = role
                self.parts = parts

        callback = build_pmi_match_gate_callback(
            output_key="ar_match",
            tool_config_id="14",
            owner_id="com@scei88.fr",
        )

        with patch("google.genai.types.Content", FakeContent),              patch("google.genai.types.Part", FakePart):
            result = callback(ctx)

        assert result is not None
        assert "ar_match" in state
        match_data = json.loads(state["ar_match"])
        assert match_data["status"] == "out_of_scope"

    def test_valid_ar_intake_calls_match_and_writes_state(self):
        """(b) valid ar_intake -> calls match_ar_to_pmi, state[ar_match] == Content text."""
        from th2customers.scei.gates import build_pmi_match_gate_callback

        intake_payload = json.dumps({
            "email_classification": "ar",
            "raison": None,
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        })
        state = {"ar_intake": intake_payload}
        ctx = _make_fake_context(state)

        mock_run_sql = _make_pmi_run_sql_cf101090()
        captured_content = {}

        class FakePart:
            def __init__(self, text): self.text = text
        class FakeContent:
            def __init__(self, role, parts):
                self.role = role
                self.parts = parts
                captured_content["content"] = self

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            return_value=("database", {"DB_TYPE": "mssql", "DB_NAME": "PMI"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            return_value=[mock_run_sql],
        ):
            callback = build_pmi_match_gate_callback(
                output_key="ar_match",
                tool_config_id="14",
                owner_id="com@scei88.fr",
            )

            with patch("google.genai.types.Content", FakeContent),                  patch("google.genai.types.Part", FakePart):
                result = callback(ctx)

        assert result is not None
        assert "ar_match" in state
        state_json = state["ar_match"]
        content_json = captured_content["content"].parts[0].text
        assert state_json == content_json, "state and Content text must be identical JSON"

        parsed = json.loads(state_json)
        assert parsed["status"] == "matched"
        assert parsed["po"] is not None

    def test_tool_config_absent_writes_sentinel_not_none(self):
        """(c) tool_config lookup fails -> sentinel __error__ written, Content returned, NOT None."""
        from th2customers.scei.gates import build_pmi_match_gate_callback

        intake_payload = json.dumps({
            "email_classification": "ar",
            "raison": None,
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        })
        state = {"ar_intake": intake_payload}
        ctx = _make_fake_context(state)

        class FakePart:
            def __init__(self, text): self.text = text
        class FakeContent:
            def __init__(self, role, parts):
                self.role = role
                self.parts = parts

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            side_effect=Exception("tool_config 14 not found"),
        ):
            callback = build_pmi_match_gate_callback(
                output_key="ar_match",
                tool_config_id="14",
                owner_id="com@scei88.fr",
            )

            with patch("google.genai.types.Content", FakeContent),                  patch("google.genai.types.Part", FakePart):
                result = callback(ctx)

        assert result is not None, "sentinel Content must be returned, not None"
        assert "ar_match" in state
        parsed = json.loads(state["ar_match"])
        assert "__error__" in parsed
        assert parsed["__error__"] == "pmi_match_failed"

    def test_upstream_skip_returns_none(self):
        """(d) upstream __skipped_upstream__ -> return None."""
        from th2customers.scei.gates import build_pmi_match_gate_callback

        intake_payload = json.dumps({"__skipped_upstream__": True, "reason": "upstream was skip"})
        state = {"ar_intake": intake_payload}
        ctx = _make_fake_context(state)

        callback = build_pmi_match_gate_callback(
            output_key="ar_match",
            tool_config_id="14",
            owner_id="com@scei88.fr",
        )
        result = callback(ctx)
        assert result is None

    def test_upstream_absent_returns_none(self):
        """(d2) upstream ar_intake missing -> return None."""
        from th2customers.scei.gates import build_pmi_match_gate_callback

        state = {}
        ctx = _make_fake_context(state)

        callback = build_pmi_match_gate_callback(
            output_key="ar_match",
            tool_config_id="14",
            owner_id="com@scei88.fr",
        )
        result = callback(ctx)
        assert result is None



    def test_tools_without_tool_run_sql_writes_sentinel(self):
        """(e) make_database_tools returns list without tool_run_sql -> sentinel __error__, NOT None."""
        from th2customers.scei.gates import build_pmi_match_gate_callback

        intake_payload = json.dumps({
            "email_classification": "ar",
            "raison": None,
            "extraction_results": {
                "commande_number_sql": "101090",
                "commande_number_display": "CF101090",
                "lines": [],
            },
        })
        state = {"ar_intake": intake_payload}
        ctx = _make_fake_context(state)

        def fake_tool_that_is_not_run_sql(sql: str) -> dict:
            return {"success": True, "data": []}
        fake_tool_that_is_not_run_sql.__name__ = "tool_db"

        class FakePart:
            def __init__(self, text): self.text = text
        class FakeContent:
            def __init__(self, role, parts):
                self.role = role
                self.parts = parts

        with patch(
            "th2customers.scei.gates.load_tool_config_params",
            return_value=("database", {"DB_TYPE": "mssql", "DB_NAME": "PMI"}),
        ), patch(
            "th2customers.scei.gates.make_database_tools",
            return_value=[fake_tool_that_is_not_run_sql],
        ):
            callback = build_pmi_match_gate_callback(
                output_key="ar_match",
                tool_config_id="14",
                owner_id="com@scei88.fr",
            )

            with patch("google.genai.types.Content", FakeContent),                  patch("google.genai.types.Part", FakePart):
                result = callback(ctx)

        assert result is not None, "sentinel Content must be returned, not None"
        assert "ar_match" in state
        parsed = json.loads(state["ar_match"])
        assert "__error__" in parsed, f"Expected sentinel __error__, got: {parsed}"
        assert parsed["__error__"] == "pmi_match_failed"

# ---------------------------------------------------------------------------
# Tests for maybe_wire_pmi_match_gate() wiring guard
# ---------------------------------------------------------------------------


class TestMaybeWirePmiMatchGate:

    def _make_matcher_details(self, output_schema_name="ARMatchPayload", output_key="ar_match", agent_tools=None):
        return {
            "output_schema_name": output_schema_name,
            "output_key": output_key,
            "agent_tools": ["tool_config14"] if agent_tools is None else agent_tools,
            "owner_id": "com@scei88.fr",
        }

    def test_wires_on_correct_matcher_agent(self):
        """Wires gate when output_schema_name==ARMatchPayload AND output_key==ar_match."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = self._make_matcher_details()
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "ar_match", agent_kwargs)

        assert result is True
        assert "before_agent_callback" in agent_kwargs

    def test_does_not_wire_on_non_matcher_schema(self):
        """Does NOT wire when output_schema_name != ARMatchPayload."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = self._make_matcher_details(output_schema_name="SCEIIntakePayload", output_key="ar_intake")
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "ar_intake", agent_kwargs)

        assert result is False
        assert "before_agent_callback" not in agent_kwargs

    def test_does_not_wire_wrong_output_key(self):
        """Does NOT wire when output_key != ar_match."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = self._make_matcher_details(output_schema_name="ARMatchPayload", output_key="something_else")
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "something_else", agent_kwargs)

        assert result is False

    def test_does_not_wire_without_tool_config(self):
        """Does NOT wire when agent_tools is empty."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = self._make_matcher_details(agent_tools=[])
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "ar_match", agent_kwargs)

        assert result is False

    def test_does_not_wire_without_output_key(self):
        """Does NOT wire when output_key is None."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = self._make_matcher_details()
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, None, agent_kwargs)

        assert result is False

    def test_wires_when_agent_tools_is_json_string(self):
        """Cas réel prod : agent_tools est une chaîne JSON, pas une liste Python.

        Avant le fix, l'itération portait sur les CARACTÈRES de la string
        -> aucun caractère ne commence par 'tool_config' -> tool_configs=[]
        -> return False (faux-négatif) -> gate jamais câblé -> LLM Qwen hallucine.
        """
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = {
            "output_schema_name": "ARMatchPayload",
            "agent_tools": '["tool_config14"]',   # format réel retourné par la DB
            "owner_id": "com@scei88.fr",
        }
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "ar_match", agent_kwargs)

        assert result is True, (
            "Le gate PMI doit se câbler même quand agent_tools est une chaîne JSON. "
            "Vérifie que maybe_wire_pmi_match_gate fait json.loads() sur les strings."
        )
        assert "before_agent_callback" in agent_kwargs

    def test_does_not_wire_when_agent_tools_empty_json_string(self):
        """agent_tools = '[]' (chaîne JSON vide) -> must NOT wire (no tool_config found)."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = {
            "output_schema_name": "ARMatchPayload",
            "agent_tools": "[]",
            "owner_id": "com@scei88.fr",
        }
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "ar_match", agent_kwargs)

        assert result is False

    def test_wires_when_agent_tools_is_json_string_multiple_tools(self):
        """agent_tools = JSON string avec plusieurs outils -> câble sur le premier tool_config."""
        from th2customers.scei.gates import maybe_wire_pmi_match_gate

        details = {
            "output_schema_name": "ARMatchPayload",
            "agent_tools": '["tool_config14", "tool_config15"]',
            "owner_id": "com@scei88.fr",
        }
        agent_kwargs = {}
        result = maybe_wire_pmi_match_gate(details, "ar_match", agent_kwargs)

        assert result is True
        assert "before_agent_callback" in agent_kwargs


# ---------------------------------------------------------------------------
# Tests for POLine schema additions
# ---------------------------------------------------------------------------


class TestPoLineSchemaAdditions:

    def test_poline_has_lcctpunet_and_lcctexpu_fields(self):
        from th2customers.scei.schemas import POLine

        line = POLine(
            lcctcode="TILCO",
            lcctrefext="821667-SF",
            lcctqty=30.0,
            lcctpunet=123.45,
            lcctexpu="",
        )
        assert line.lcctpunet == 123.45
        assert line.lcctexpu == ""

    def test_poline_price_fields_optional(self):
        from th2customers.scei.schemas import POLine

        line = POLine(lcctrefext="REF", lcctqty=1.0)
        assert line.lcctpunet is None
        assert line.lcctexpu is None

    def test_armatchpayload_lines_serialise_with_price(self):
        from th2customers.scei.schemas import ARMatchPayload, POHeader, POLine

        payload = ARMatchPayload(
            status="matched",
            po=POHeader(ecktsoc="SC", ecktnumero="101090", ecctcode="TILCO", ecctnom="TILCO FRANCE SAS"),
            lines=[POLine(lcctrefext="821667-SF", lcctqty=30.0, lcctpunet=123.45, lcctexpu="")],
            diagnostic="matched via ECKTNUMERO exact",
        )
        import json as _json
        data = _json.loads(payload.model_dump_json())
        assert abs(data["lines"][0]["lcctpunet"] - 123.45) < 0.001
        assert data["lines"][0]["lcctexpu"] == ""


# ---------------------------------------------------------------------------
# Tests for scei_v2 prompt fix
# ---------------------------------------------------------------------------


class TestSceiV2MatcherPromptFix:

    def test_matcher_prompt_uses_tool_run_sql_not_pmi_suffix(self):
        """_MATCHER_PROMPT must not reference stale tool name tool_run_sql_pmi."""
        from th2customers.scei.templates.scei_v2 import _MATCHER_PROMPT

        assert "tool_run_sql_pmi" not in _MATCHER_PROMPT, (
            "Prompt still references stale tool name tool_run_sql_pmi. "
            "Should be tool_run_sql."
        )


class TestAuthoritativeCommandeNumber:
    """Bug #2 (live 2026-05-27, SOCOMEC): the CF in the email/PDF is the
    authoritative SCEI PO number; the intake LLM drifts to the supplier ref
    ('3BS638919') which matches a wrong PMI order. _authoritative_commande_number
    forces the deterministic CF from the text when exactly one is present."""

    def test_overrides_when_llm_diverges_from_single_pdf_cf(self):
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        intake = {"extraction_results": {"commande_number_sql": "3BS638919"}}
        pdf = "Accusé de réception SOCOMEC ... CF101173 / Notre commande : 3BS638919"
        out = _authoritative_commande_number(intake, pdf)
        assert out["extraction_results"]["commande_number_sql"] == "101173"

    def test_noop_when_llm_already_matches_recovered(self):
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        intake = {"extraction_results": {"commande_number_sql": "101173"}}
        out = _authoritative_commande_number(intake, "ref CF101173 bla")
        assert out["extraction_results"]["commande_number_sql"] == "101173"

    def test_noop_when_no_cf_in_text(self):
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        intake = {"extraction_results": {"commande_number_sql": "3BS638919"}}
        out = _authoritative_commande_number(intake, "aucun numero SCEI ici")
        assert out["extraction_results"]["commande_number_sql"] == "3BS638919"

    def test_noop_when_pdf_text_none(self):
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        intake = {"extraction_results": {"commande_number_sql": "3BS638919"}}
        out = _authoritative_commande_number(intake, None)
        assert out["extraction_results"]["commande_number_sql"] == "3BS638919"

    def test_noop_when_multiple_distinct_cf_in_text(self):
        """Conservative: 2+ distinct CF -> _recover returns None -> no override
        (a multi-order PDF must never trigger a wrong forced match)."""
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        intake = {"extraction_results": {"commande_number_sql": "3BS638919"}}
        pdf = "commande CF101173 ... suite à CF091022 ..."
        out = _authoritative_commande_number(intake, pdf)
        assert out["extraction_results"]["commande_number_sql"] == "3BS638919"

    def test_does_not_mutate_input(self):
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        intake = {"extraction_results": {"commande_number_sql": "3BS638919"}, "other": 1}
        _authoritative_commande_number(intake, "CF101173")
        assert intake["extraction_results"]["commande_number_sql"] == "3BS638919"

    def test_handles_missing_extraction_results(self):
        from th2customers.scei.superagents.scei_pmi_match import _authoritative_commande_number
        out = _authoritative_commande_number({}, "CF101173")
        assert out["extraction_results"]["commande_number_sql"] == "101173"
