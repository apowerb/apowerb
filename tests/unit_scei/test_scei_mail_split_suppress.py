"""Tests TDD — suppression mail acheteur sur les lignes filles (fan-out).

Comportement attendu : si `state["scei_suppress_buyer_mail"] is True`,
tool_send_scei_mail doit :
  1. Envoyer RÉELLEMENT vers l'adresse interne (redirect_addr), PAS vers l'acheteur.
  2. Ne JAMAIS appeler _get_db_engine (pas de whitelist pour l'interne).
  3. Retourner {"success": True, "mode": "redirected_split", "audit_id": None}.

Lancer: .venv/bin/python -m pytest tests/unit_scei/test_scei_mail_split_suppress.py -q
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class FakeToolContext:
    """Simule le ToolContext ADK avec un state dict."""

    def __init__(self, state: dict | None = None):
        self.state = state or {}


class TestSplitChildMailSuppression:
    def _make_ctx(self, suppress: bool = True) -> FakeToolContext:
        return FakeToolContext(state={"scei_suppress_buyer_mail": suppress})

    def test_returns_redirected_split_mode(self):
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        with patch.object(scei_mail, "_send_outlook", return_value={"ok": True}):
            result = scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
        assert result["mode"] == "redirected_split"

    def test_returns_success_true(self):
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        with patch.object(scei_mail, "_send_outlook", return_value={"ok": True}):
            result = scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
        assert result["success"] is True

    def test_audit_id_is_none(self):
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        with patch.object(scei_mail, "_send_outlook", return_value={"ok": True}):
            result = scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
        assert result["audit_id"] is None

    def test_suppress_sends_to_redirect_addr_not_buyer(self):
        """IMPORTANT 3 : le mail DOIT être envoyé vers redirect_addr (jamais l'acheteur).

        Ce test aurait attrapé le bug : l'ancien code faisait un return sans appeler
        _send_outlook, perdant l'observabilité et aveuglant le dédup futur.
        """
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        redirect_addr = scei_mail._dry_run_redirect_to()

        with patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as mock_send:
            scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args
            # Premier arg positionnel ou kwarg 'to'
            actual_to = call_kwargs.kwargs.get("to") or call_kwargs.args[0]
            assert actual_to == redirect_addr, (
                f"_send_outlook doit être appelé avec to={redirect_addr!r}, "
                f"pas {actual_to!r}"
            )
            # Jamais l'acheteur
            assert actual_to != "buyer@scei88.fr"

    def test_suppress_subject_prefixed(self):
        """Le subject doit être préfixé [SPLIT->interne] pour identification visuelle."""
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        with patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as mock_send:
            scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
            call_kwargs = mock_send.call_args
            actual_subject = call_kwargs.kwargs.get("subject") or call_kwargs.args[1]
            assert "[SPLIT" in actual_subject, (
                f"Subject devrait contenir [SPLIT...], got: {actual_subject!r}"
            )

    def test_no_db_engine_called(self):
        """Pas de query DB pour le whitelist/audit quand supprimé."""
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        with patch.object(scei_mail, "_get_db_engine") as mock_engine, \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}):
            scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
            mock_engine.assert_not_called()

    def test_suppress_no_cc_escalation(self):
        """Aucun CC d'escalade sur les lignes filles (même si non_conforme)."""
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=True)
        with patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as mock_send:
            scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
            call_kwargs = mock_send.call_args
            actual_cc = call_kwargs.kwargs.get("cc")
            assert actual_cc is None, f"CC devrait être None sur split, got: {actual_cc!r}"

    def test_false_suppress_flag_does_not_short_circuit(self):
        """suppress=False → pipeline normal (DB indispo → db_unavailable)."""
        from th2customers.scei.tools import scei_mail

        ctx = self._make_ctx(suppress=False)
        with patch.object(scei_mail, "_get_db_engine", return_value=None):
            result = scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=ctx,
            )
        # Sans suppression, on suit le chemin normal → db_unavailable
        assert result["mode"] != "redirected_split"
        assert result["success"] is False
        assert result["reason"] == "db_unavailable"

    def test_no_state_does_not_suppress(self):
        """Pas de state → pipeline normal, pas de court-circuit."""
        from th2customers.scei.tools import scei_mail

        with patch.object(scei_mail, "_get_db_engine", return_value=None):
            result = scei_mail.tool_send_scei_mail(
                to="buyer@scei88.fr",
                subject="AR CF101082",
                body="body text",
                tool_context=None,
            )
        assert result["mode"] != "redirected_split"
