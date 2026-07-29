"""GARDE-FOU #1 — TDD schemas: ARData + ARMatchPayload nouveaux champs.

RED phase: ces tests échouent AVANT les modifications des schemas.
GREEN phase: après ajout des champs optionnels.

Invariants:
- ARData doit accepter fournisseur_nom/fournisseur_code optionnels.
- Les payloads existants sans ces champs continuent de valider.
- ARMatchPayload doit accepter supplier_mismatch optionnel (défaut False).
"""
from __future__ import annotations

import json
import pytest


class TestARDataFournisseurChamps:
    """ARData accepte les champs fournisseur optionnels sans casser l'existant."""

    def test_ardata_sans_fournisseur_valide(self):
        """Non-régression: ARData sans fournisseur_nom/fournisseur_code passe."""
        from th2customers.scei.schemas import ARData

        ar = ARData(
            commande_number_display="CF101090",
            commande_number_sql="101090",
        )
        assert ar.fournisseur_nom is None
        assert ar.fournisseur_code is None

    def test_ardata_avec_fournisseur_nom(self):
        """ARData accepte fournisseur_nom non-nul."""
        from th2customers.scei.schemas import ARData

        ar = ARData(
            commande_number_display="CF101090",
            commande_number_sql="101090",
            fournisseur_nom="TILCO FRANCE SAS",
        )
        assert ar.fournisseur_nom == "TILCO FRANCE SAS"
        assert ar.fournisseur_code is None

    def test_ardata_avec_fournisseur_code(self):
        """ARData accepte fournisseur_code non-nul."""
        from th2customers.scei.schemas import ARData

        ar = ARData(
            commande_number_display="CF101090",
            commande_number_sql="101090",
            fournisseur_code="TILCO",
        )
        assert ar.fournisseur_code == "TILCO"

    def test_ardata_json_roundtrip_sans_fournisseur(self):
        """Roundtrip JSON préserve None sans écrire les clés si None."""
        from th2customers.scei.schemas import ARData

        ar = ARData(
            commande_number_display="CF101090",
            commande_number_sql="101090",
        )
        dumped = json.loads(ar.model_dump_json())
        back = ARData.model_validate(dumped)
        assert back.fournisseur_nom is None
        assert back.fournisseur_code is None

    def test_ardata_json_roundtrip_avec_fournisseur(self):
        """Roundtrip JSON préserve fournisseur_nom."""
        from th2customers.scei.schemas import ARData

        ar = ARData(
            commande_number_display="CF101090",
            commande_number_sql="101090",
            fournisseur_nom="TILCO FRANCE SAS",
            fournisseur_code="TILCO",
        )
        dumped = json.loads(ar.model_dump_json())
        back = ARData.model_validate(dumped)
        assert back.fournisseur_nom == "TILCO FRANCE SAS"
        assert back.fournisseur_code == "TILCO"

    def test_scei_intake_payload_serialise_fournisseur(self):
        """SCEIIntakePayload préserve fournisseur_nom via ARData embedded."""
        from th2customers.scei.schemas import (
            SCEIIntakePayload,
            ARData,
        )

        p = SCEIIntakePayload(
            email_classification="ar",
            extraction_results=ARData(
                commande_number_display="CF101090",
                commande_number_sql="101090",
                fournisseur_nom="TILCO FRANCE SAS",
            ),
        )
        dumped = p.model_dump()
        assert dumped["extraction_results"]["fournisseur_nom"] == "TILCO FRANCE SAS"


class TestARMatchPayloadSupplierMismatch:
    """ARMatchPayload accepte supplier_mismatch optionnel (défaut False)."""

    def test_payload_sans_supplier_mismatch(self):
        """Non-régression: payload matched sans supplier_mismatch -> False par défaut."""
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(
            status="matched",
            diagnostic="matched via ECKTNUMERO='101090'",
        )
        assert p.supplier_mismatch is False

    def test_payload_avec_supplier_mismatch_true(self):
        """supplier_mismatch=True est accepté."""
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(
            status="matched",
            diagnostic="matched via ECKTNUMERO='101090'",
            supplier_mismatch=True,
        )
        assert p.supplier_mismatch is True

    def test_payload_non_rapproche_supplier_mismatch_false(self):
        """Non-régression: non_rapproche sans supplier_mismatch -> False."""
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(status="non_rapproche", diagnostic="PO non trouvé")
        assert p.supplier_mismatch is False

    def test_json_roundtrip_supplier_mismatch(self):
        """Roundtrip JSON préserve supplier_mismatch=True."""
        from th2customers.scei.schemas import ARMatchPayload

        p = ARMatchPayload(
            status="matched",
            diagnostic="x",
            supplier_mismatch=True,
        )
        back = ARMatchPayload.model_validate_json(p.model_dump_json())
        assert back.supplier_mismatch is True
