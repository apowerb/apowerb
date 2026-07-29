"""Tests for SCEI Pydantic schemas — contracts between sub-agents.

Each sub-agent produces a JSON payload that ADK stores in session.state.
The next sub-agent reads it back via the `{ar_intake}` syntax. The
contract is validated by Pydantic on the receiving end (via
before_model_callback) — see [[project_th2agent_pr173]] for why we don't
rely on ADK's output_schema (incompatible with tools).
"""

from __future__ import annotations

import json
import pytest


# ---------------------------------------------------------------------------
# ARIntakePayload — produced by scei_ar_intake
# ---------------------------------------------------------------------------


class TestARIntakePayload:
    def test_skip_with_reason(self):
        from th2customers.scei.schemas import ARIntakePayload

        p = ARIntakePayload(status="skip", raison="non-AR admin mail")
        assert p.status == "skip"
        assert p.raison == "non-AR admin mail"
        assert p.ar is None

    def test_process_with_ar(self):
        from th2customers.scei.schemas import (
            ARIntakePayload,
            ARData,
            ARLine,
        )

        ar = ARData(
            commande_number_display="CF098983",
            commande_number_sql="098983",
            societe_inferred="SCEI",
            lines=[
                ARLine(
                    ligne_numero=1,
                    ref_fournisseur="REF-A",
                    qty=10.0,
                    unit_price_ht=12.5,
                    expected_delivery_yyyymmdd="20260601",
                )
            ],
        )
        p = ARIntakePayload(status="process", ar=ar)
        assert p.status == "process"
        assert p.ar.commande_number_display == "CF098983"
        assert len(p.ar.lines) == 1

    def test_status_must_be_skip_or_process(self):
        from th2customers.scei.schemas import ARIntakePayload

        with pytest.raises(ValueError):
            ARIntakePayload(status="invalid")

    def test_json_roundtrip(self):
        """Critical: payload must survive str(session.state[k]) round-trip."""
        from th2customers.scei.schemas import (
            ARIntakePayload,
            ARData,
        )

        ar = ARData(
            commande_number_display="CF111111",
            commande_number_sql="111111",
            societe_inferred=None,
            lines=[],
        )
        p = ARIntakePayload(status="process", ar=ar)
        s = p.model_dump_json()
        back = ARIntakePayload.model_validate_json(s)
        assert back == p


# ---------------------------------------------------------------------------
# ARMatchPayload — produced by scei_ar_matcher
# ---------------------------------------------------------------------------


class TestARMatchPayload:
    def test_matched(self):
        from th2customers.scei.schemas import (
            ARMatchPayload,
            POHeader,
            POLine,
        )

        po = POHeader(
            ecktsoc="SCEI",
            ecktnumero="098983",
            ecctcode="FOURN-X",
            ecctnom="Tilco",
        )
        lines = [POLine(lcctcode="FOURN-X", lcctrefext="REF-A", lcctqty=10.0)]
        p = ARMatchPayload(
            status="matched",
            po=po,
            lines=lines,
            diagnostic="2 lignes OK",
        )
        assert p.status == "matched"
        assert p.po.ecktnumero == "098983"
        assert len(p.lines) == 1

    def test_matched_preserves_lcctcodart(self):
        """Regression (faux non_rapproche 28/05) : ARMatchPayload NE DOIT PAS
        dropper lcctcodart. Sans ce champ dans POLine, Pydantic le supprimait
        silencieusement entre matcher et recorder -> 0 ligne matchee (LCCTREFEXT
        vide en PMI, jointure du recorder sur lcctcodart) -> faux non_rapproche."""
        from th2customers.scei.schemas import ARMatchPayload, POLine

        line = POLine(
            lcctcode="100001", lcctrefext="", lcctcodart="555555830001",
            lcctqty=1.0, lcctpunet=76.38, lcctexpu="",
        )
        assert line.lcctcodart == "555555830001"

        # Point exact du drop : la validation complete du payload
        p = ARMatchPayload(
            status="matched",
            lines=[{
                "lcctcode": "100001", "lcctrefext": "",
                "lcctcodart": "555555830001", "lcctqty": 1.0,
                "lcctpunet": 76.38, "lcctexpu": "",
            }],
            diagnostic="1 ligne",
        )
        assert p.lines[0].lcctcodart == "555555830001"
        # survit au round-trip JSON (state ar_match -> recorder)
        back = ARMatchPayload.model_validate_json(p.model_dump_json())
        assert back.lines[0].lcctcodart == "555555830001"

    def test_non_rapproche(self):
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(
            status="non_rapproche",
            diagnostic="CF0916 absent de PMI",
        )
        assert p.status == "non_rapproche"
        assert p.po is None
        assert p.lines == []

    def test_out_of_scope(self):
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(
            status="out_of_scope",
            diagnostic="Fournisseur DHL exclu",
        )
        assert p.status == "out_of_scope"

    def test_status_must_be_one_of_three(self):
        from th2customers.scei.schemas import ARMatchPayload

        with pytest.raises(ValueError):
            ARMatchPayload(status="bogus", diagnostic="x")

    def test_json_roundtrip(self):
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(status="non_rapproche", diagnostic="x")
        back = ARMatchPayload.model_validate_json(p.model_dump_json())
        assert back == p


# ---------------------------------------------------------------------------
# ARRecordPayload — produced by scei_ar_recorder
# ---------------------------------------------------------------------------


class TestARRecordPayload:
    def test_ok(self):
        from th2customers.scei.schemas import ARRecordPayload

        p = ARRecordPayload(
            commande_id="UUID-...",
            lignes_inserees=3,
            status_final="OK",
            erreurs=[],
        )
        assert p.status_final == "OK"
        assert p.lignes_inserees == 3

    def test_nok_with_errors(self):
        from th2customers.scei.schemas import ARRecordPayload

        p = ARRecordPayload(
            commande_id="UUID-...",
            lignes_inserees=2,
            status_final="NOK",
            erreurs=["LINE_NOT_IN_PO ligne 3"],
        )
        assert p.status_final == "NOK"
        assert len(p.erreurs) == 1

    def test_skipped(self):
        """When upstream skipped, recorder also skips — no row created."""
        from th2customers.scei.schemas import ARRecordPayload

        p = ARRecordPayload(status_final="SKIPPED")
        assert p.commande_id is None
        assert p.lignes_inserees == 0
        assert p.erreurs == []

    def test_status_must_be_one_of_three(self):
        from th2customers.scei.schemas import ARRecordPayload

        with pytest.raises(ValueError):
            ARRecordPayload(status_final="WAT")


# ---------------------------------------------------------------------------
# ARNotifyPayload — produced by scei_ar_notifier (dormant until PR #148 lift)
# ---------------------------------------------------------------------------


class TestARNotifyPayload:
    def test_draft_only_default(self):
        """Until mail auto re-opens (cf cadrage 12/05), sent=False forced."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="AR CF111 — écart prix",
            draft_body="Bonjour, écart ...",
            to=["acheteur@scei88.fr"],
        )
        assert p.sent is False  # default

    def test_sent_true_when_explicitly(self):
        """Post mail auto re-opening, sent=True will be set by the notifier
        after a successful tool_send_outlook_email call."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="x",
            draft_body="y",
            to=["a@b.fr"],
            sent=True,
        )
        assert p.sent is True


# ---------------------------------------------------------------------------
# SCHEMA_REGISTRY — for dynamic lookup by output_schema_name DB column
# ---------------------------------------------------------------------------


class TestSchemaRegistry:
    def test_all_payloads_registered(self):
        from th2customers.scei.schemas import SCEI_SCHEMA_REGISTRY

        expected = {
            "ARIntakePayload",
            "SCEIIntakePayload",
            "ARMatchPayload",
            "ARRecordPayload",
            "ARNotifyPayload",
        }
        assert set(SCEI_SCHEMA_REGISTRY.keys()) == expected

    def test_lookup_returns_pydantic_class(self):
        from pydantic import BaseModel
        from th2customers.scei.schemas import SCEI_SCHEMA_REGISTRY

        cls = SCEI_SCHEMA_REGISTRY["ARIntakePayload"]
        assert issubclass(cls, BaseModel)
        instance = cls(status="skip", raison="x")
        assert instance.status == "skip"

    def test_unknown_name_not_in_registry(self):
        from th2customers.scei.schemas import SCEI_SCHEMA_REGISTRY

        assert "bogus" not in SCEI_SCHEMA_REGISTRY


class TestSCEIIntakePayload:
    """v2 intake contract (deterministic classification + extraction)."""

    def test_not_ar_serialises_empty_extraction(self):
        from th2customers.scei.schemas import SCEIIntakePayload

        p = SCEIIntakePayload(email_classification="not_ar", raison="no_pdf")
        d = p.model_dump()
        assert d["email_classification"] == "not_ar"
        assert d["extraction_results"] == {}  # the {} contract, not null

    def test_ar_requires_extraction(self):
        from th2customers.scei.schemas import SCEIIntakePayload

        with pytest.raises(Exception):
            SCEIIntakePayload(email_classification="ar")

    def test_not_ar_rejects_extraction(self):
        from th2customers.scei.schemas import (
            SCEIIntakePayload,
            ARData,
        )

        with pytest.raises(Exception):
            SCEIIntakePayload(
                email_classification="not_ar",
                extraction_results=ARData(
                    commande_number_display="CF1", commande_number_sql="1"
                ),
            )

    def test_ar_with_extraction_is_json_safe(self):
        from th2customers.scei.schemas import (
            SCEIIntakePayload,
            ARData,
            ARLine,
        )

        p = SCEIIntakePayload(
            email_classification="ar",
            extraction_results=ARData(
                commande_number_display="CF098983",
                commande_number_sql="098983",
                lines=[ARLine(ligne_numero=1, qty=2, unit_price_ht=10.0)],
            ),
        )
        # after_agent_callback does json.dumps(model.model_dump())
        rt = json.loads(json.dumps(p.model_dump()))
        assert rt["email_classification"] == "ar"
        assert rt["extraction_results"]["commande_number_display"] == "CF098983"
        assert rt["extraction_results"]["lines"][0]["qty"] == 2

    def test_not_ar_round_trip_from_llm_output(self):
        """Exact callback path: the LLM emits extraction_results: {} for
        not_ar; model_validate(json.loads(...)) must NOT raise."""
        from th2customers.scei.schemas import SCEIIntakePayload

        raw = json.dumps(
            {"email_classification": "not_ar", "raison": "facture",
             "extraction_results": {}}
        )
        p = SCEIIntakePayload.model_validate(json.loads(raw))
        assert p.email_classification == "not_ar"
        assert p.extraction_results is None
        assert p.model_dump()["extraction_results"] == {}
        # full round-trip stable
        assert SCEIIntakePayload.model_validate(p.model_dump()).model_dump() == p.model_dump()

    def test_ar_round_trip_from_llm_output(self):
        from th2customers.scei.schemas import SCEIIntakePayload

        raw = json.dumps({
            "email_classification": "ar",
            "extraction_results": {
                "commande_number_display": "CF098983",
                "commande_number_sql": "098983",
                "lines": [{"ligne_numero": 1, "qty": 2, "unit_price_ht": 10.0}],
            },
        })
        p = SCEIIntakePayload.model_validate(json.loads(raw))
        assert p.extraction_results.commande_number_display == "CF098983"
        assert SCEIIntakePayload.model_validate(p.model_dump()).model_dump() == p.model_dump()

    def test_registered_for_dynamic_lookup(self):
        from th2customers.scei.schemas import (
            SCEI_SCHEMA_REGISTRY,
            SCEIIntakePayload,
        )

        assert SCEI_SCHEMA_REGISTRY["SCEIIntakePayload"] is SCEIIntakePayload


class TestARLinePositionalCoercion:
    """Regression 2026-05-21: the intake LLM sometimes emits a line as a
    positional array; ARLine must coerce it instead of failing validation
    (which dropped the whole AR before it reached the recorder)."""

    def test_arline_coerces_positional_array(self):
        from th2customers.scei.schemas import ARLine

        line = ARLine.model_validate([1, "RE-110-35", 120, 5.15, "20260612"])
        assert line.ligne_numero == 1
        assert line.ref_fournisseur == "RE-110-35"
        assert line.qty == 120
        assert line.unit_price_ht == 5.15
        assert line.expected_delivery_yyyymmdd == "20260612"

    def test_arline_named_object_still_works(self):
        from th2customers.scei.schemas import ARLine

        line = ARLine.model_validate(
            {"ligne_numero": 2, "ref_fournisseur": "X", "qty": 3,
             "unit_price_ht": 1.0, "expected_delivery_yyyymmdd": "20260101"}
        )
        assert line.ligne_numero == 2

    def test_intake_payload_with_positional_lines_validates(self):
        """Exact 3116 regression: SCEIIntakePayload whose lines are arrays
        must now validate (was: validation_failed -> AR dropped)."""
        from th2customers.scei.schemas import SCEIIntakePayload

        p = SCEIIntakePayload.model_validate({
            "email_classification": "ar",
            "raison": None,
            "extraction_results": {
                "commande_number_display": "CF024512",
                "commande_number_sql": "024512",
                "societe_inferred": "SCEI",
                "lines": [
                    [1, "RE-110-35", 120, 5.15, "20260612"],
                    [2, "RE-130-45", 180, 5.80, "20260612"],
                ],
            },
        })
        assert p.email_classification == "ar"
        assert p.extraction_results.lines[0].ref_fournisseur == "RE-110-35"
        assert p.extraction_results.lines[1].qty == 180


class TestARLineCoercionNegatives:
    """Negative cases: a wrong-length array must NOT be silently mis-mapped."""

    def test_array_len_4_rejected(self):
        import pytest
        from pydantic import ValidationError
        from th2customers.scei.schemas import ARLine

        with pytest.raises(ValidationError):
            ARLine.model_validate([1, "X", 1, 1.0])

    def test_array_len_6_rejected(self):
        import pytest
        from pydantic import ValidationError
        from th2customers.scei.schemas import ARLine

        with pytest.raises(ValidationError):
            ARLine.model_validate([1, "X", 1, 1.0, "20260612", "extra"])

    def test_none_rejected(self):
        import pytest
        from pydantic import ValidationError
        from th2customers.scei.schemas import ARLine

        with pytest.raises(ValidationError):
            ARLine.model_validate(None)
