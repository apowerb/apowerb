"""FIX 3 (passe 3) TDD : ARNotifyPayload doit accepter human_review_required.

RED phase : ce test échoue AVANT l ajout du champ (ValidationError ou KeyError).
GREEN phase : apres ajout de human_review_required: bool = False dans ARNotifyPayload.

Invariants :
- model_validate avec human_review_required=True conserve le champ.
- model_validate SANS human_review_required ne leve pas d erreur (defaut False).
- Les payloads existants restent valides.
"""
from __future__ import annotations

import json
import pytest


class TestARNotifyPayloadHumanReviewRequired:
    """human_review_required est un champ optionnel bool (defaut False)."""

    def test_validate_with_human_review_required_true(self):
        """model_validate avec human_review_required=True conserve le champ."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload.model_validate({
            "draft_subject": "AR CF101090",
            "draft_body": "Corps du mail",
            "to": ["supplier@example.com"],
            "sent": False,
            "human_review_required": True,
        })
        assert p.human_review_required is True

    def test_validate_without_human_review_required_defaults_false(self):
        """Les payloads existants sans human_review_required valident (defaut False)."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload.model_validate({
            "draft_subject": "AR CF101090",
            "draft_body": "Corps du mail",
            "to": ["supplier@example.com"],
            "sent": False,
        })
        assert p.human_review_required is False

    def test_constructor_default_false(self):
        """Construction directe sans human_review_required -> False."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="AR",
            draft_body="body",
            to=["x@y.com"],
        )
        assert p.human_review_required is False

    def test_json_roundtrip_with_human_review_required(self):
        """Roundtrip JSON preserve human_review_required=True."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="AR",
            draft_body="body",
            to=["x@y.com"],
            sent=False,
            human_review_required=True,
        )
        back = ARNotifyPayload.model_validate(json.loads(p.model_dump_json()))
        assert back.human_review_required is True
        assert back.sent is False

    def test_gate_payload_validates_against_schema(self):
        """Simule le payload ecrit par build_supplier_mismatch_gate_callback."""
        from th2customers.scei.schemas import ARNotifyPayload

        gate_output = {
            "sent": False,
            "human_review_required": True,
            "draft_subject": "[REVUE MANUELLE REQUISE] AR CF101090",
            "draft_body": "Fournisseur AR diverge du PMI.",
            "to": [],
        }
        p = ARNotifyPayload.model_validate(gate_output)
        assert p.human_review_required is True
        assert p.sent is False


class TestARNotifyPayloadExistingBehaviourUnchanged:
    """Non-regression : comportements existants inchanges."""

    def test_minimal_payload_still_validates(self):
        """Payload minimal sans sent ni human_review_required passe."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="Objet",
            draft_body="Corps",
            to=["a@b.com"],
        )
        assert p.sent is False
        assert p.human_review_required is False

    def test_sent_true_payload_still_validates(self):
        """Payload avec sent=True reste valide."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="Objet",
            draft_body="Corps",
            to=["a@b.com"],
            sent=True,
        )
        assert p.sent is True
        assert p.human_review_required is False

    def test_model_dump_contains_human_review_required(self):
        """model_dump inclut human_review_required dans la sortie."""
        from th2customers.scei.schemas import ARNotifyPayload

        p = ARNotifyPayload(
            draft_subject="Objet",
            draft_body="Corps",
            to=["a@b.com"],
            human_review_required=True,
        )
        d = p.model_dump()
        assert "human_review_required" in d
        assert d["human_review_required"] is True
