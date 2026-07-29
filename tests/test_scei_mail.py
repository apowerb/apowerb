"""Tests for the SCEI-specific mail wrapper tool.

This tool lives in ``portfolio/scei_mail.py`` because its business logic
(Commanditaires whitelist + scei_mail_audit logging) is SCEI-specific.
That's acceptable per the project rule: code linked to a specific agent
template is OK; only generic core code must stay client-agnostic.

The guard layer enforces, in this order:
1. Once-per-run counter via session.state (anti-cascade like the 2026-05-11
   phantom incident).
2. Recipient whitelist via SELECT on dbo.Commanditaires (anti-hallucinated
   address bug — LLM cannot bypass Python code).
3. Atomic audit log INSERT BEFORE the actual send (no send = no log skip).
4. Dry-run flag (SCEI_MAIL_AUTO_DRY_RUN env var) — when true, redirect
   to ops@scei88.fr and log mode='dry_run' instead of real Graph POST.

The LLM ONLY sees ``tool_send_scei_mail`` in its toolbox; it cannot
call ``outlook_mail.tool_send_outlook_email`` directly to bypass the
guards (the SCEI notifier template omits the latter from agent_tools).
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(state: dict | None = None):
    ctx = MagicMock()
    state_dict = dict(state or {})

    class _State:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=None):
            return self._d.get(k, default)

        def __setitem__(self, k, v):
            self._d[k] = v

        def __getitem__(self, k):
            return self._d[k]

        def __contains__(self, k):
            return k in self._d

    ctx.state = _State(state_dict)
    return ctx, state_dict


def _mock_engine_with_whitelist(allowed_emails: list[str]):
    """Mock SQLAlchemy engine whose Commanditaires query returns the given
    emails as Actif=1."""
    import contextlib

    @contextlib.contextmanager
    def connect():
        conn = MagicMock()
        result = MagicMock()

        def execute(stmt, params=None):
            sql = str(stmt).lower()
            if "commanditaires" in sql:
                email = (params or {}).get("email", "").lower()
                rows = (
                    [(email,)]
                    if email in [e.lower() for e in allowed_emails]
                    else []
                )
                r = MagicMock()
                r.fetchall.return_value = rows
                r.scalar.return_value = rows[0][0] if rows else None
                r.first.return_value = rows[0] if rows else None
                return r
            elif "scei_mail_audit" in sql and "insert" in sql:
                r = MagicMock()
                r.scalar.return_value = 42  # audit_id returned by RETURNING
                r.lastrowid = 42
                r.first.return_value = (42,)
                return r
            elif "scei_mail_audit" in sql and "select" in sql:
                # Dedup 24h check: no duplicate by default in unit tests
                r = MagicMock()
                r.first.return_value = None
                return r
            return result

        conn.execute.side_effect = execute
        conn.commit = MagicMock()
        yield conn

    engine = MagicMock()
    engine.connect = connect
    engine.begin = connect
    return engine


# ---------------------------------------------------------------------------
# _validate_recipient
# ---------------------------------------------------------------------------


class TestValidateRecipient:
    def test_recipient_in_whitelist_active_passes(self):
        from th2customers.scei.tools.scei_mail import _validate_recipient

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        assert _validate_recipient("acheteur@scei88.fr", engine) is True

    def test_recipient_not_in_whitelist_refused(self):
        from th2customers.scei.tools.scei_mail import _validate_recipient

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        assert _validate_recipient("hacker@evil.com", engine) is False

    def test_empty_recipient_refused(self):
        from th2customers.scei.tools.scei_mail import _validate_recipient

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        assert _validate_recipient("", engine) is False
        assert _validate_recipient(None, engine) is False

    def test_case_insensitive_match(self):
        """Email comparison must be case-insensitive."""
        from th2customers.scei.tools.scei_mail import _validate_recipient

        engine = _mock_engine_with_whitelist(["Acheteur@SCEI88.fr"])
        assert _validate_recipient("ACHETEUR@scei88.fr", engine) is True


# ---------------------------------------------------------------------------
# _is_dry_run
# ---------------------------------------------------------------------------


class TestIsDryRun:
    def test_default_is_dry_run(self):
        """Safety default: if env var unset, we ARE in dry-run."""
        from th2customers.scei.tools.scei_mail import _is_dry_run

        with patch.dict("os.environ", {}, clear=True):
            assert _is_dry_run() is True

    def test_explicit_true(self):
        from th2customers.scei.tools.scei_mail import _is_dry_run

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "true"}):
            assert _is_dry_run() is True

    def test_explicit_false(self):
        from th2customers.scei.tools.scei_mail import _is_dry_run

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}):
            assert _is_dry_run() is False

    def test_case_insensitive(self):
        from th2customers.scei.tools.scei_mail import _is_dry_run

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "FALSE"}):
            assert _is_dry_run() is False


# ---------------------------------------------------------------------------
# tool_send_scei_mail — the full path
# ---------------------------------------------------------------------------


class TestToolSendSceiMail:
    def test_recipient_not_whitelisted_refused_no_outlook_call(self):
        """Most critical anti-phantom: hallucinated recipient never reaches
        Outlook even if the LLM tries."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()

        with patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="hacker@evil.com",
                subject="x",
                body="y",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert result["success"] is False
        assert result["reason"] == "recipient_not_whitelisted"
        send.assert_not_called()

    def test_once_per_run_blocks_second_send(self):
        """Even if first send succeeded, second is refused."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, state = _ctx({"scei_mail_sent_count": 1})

        with patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="x",
                body="y",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert result["success"] is False
        assert result["reason"] == "once_per_run_exceeded"
        send.assert_not_called()

    def test_dry_run_sends_to_redirect_not_buyer(self):
        """SCEI_MAIL_AUTO_DRY_RUN=true → the mail IS sent for real, but to
        the redirect address (default david.gnaglo@thaink2.com), never the
        buyer; subject carries the [DRY-RUN -> buyer] prefix; mode=dry_run."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, state = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "true"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111",
                body="...",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert result["success"] is True
        assert result["mode"] == "dry_run"
        send.assert_called_once()
        _, kwargs = send.call_args
        assert kwargs.get("to") == "david.gnaglo@thaink2.com"
        assert kwargs.get("cc") is None
        assert kwargs.get("subject", "").startswith("[DRY-RUN -> acheteur@scei88.fr]")
        # Counter incremented
        assert state["scei_mail_sent_count"] == 1

    def test_live_conforme_sends_no_mail(self):
        """Cadrage: a conforme AR triggers NO mail (statut OK + log only).
        The conforme guard short-circuits before any send."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, state = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", return_value="conforme"), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111",
                body="...",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        # Cadrage: a conforme AR triggers NO mail (statut OK + log only).
        assert result["success"] is True
        assert result["reason"] == "conforme_no_mail"
        send.assert_not_called()

    def test_live_nonconforme_adds_escalation_cc(self):
        """LIVE + StatutGlobal == non_conforme → escalation addresses CC'd
        (default c.roussel + j.fruchart), decided deterministically."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", return_value="non_conforme"), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="AR CF111 - non_conforme",
                body="...",
                commande_id="5",
                tool_context=ctx,
            )

        assert result["success"] is True
        _, kwargs = send.call_args
        assert kwargs.get("cc") == "david.gnaglo@thaink2.com,c.roussel@scei88.fr,j.fruchart@scei88.fr", (
            f"non_conforme must CC the escalation addresses, got {kwargs}"
        )

    def test_live_nonconforme_cc_override_env(self):
        """SCEI_NONCONFORME_CC overrides the default escalation list."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false",
                                       "SCEI_NONCONFORME_CC": "a@scei88.fr,b@scei88.fr"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", return_value="non_conforme"), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send:
            scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr", subject="x", body="y",
                commande_id="5", tool_context=ctx,
            )

        _, kwargs = send.call_args
        assert kwargs.get("cc") == "a@scei88.fr,b@scei88.fr"

    def test_lookup_uses_state_commande_id_over_param(self):
        """The StatutGlobal lookup uses the deterministic Id written by the
        recorder into state['ar_commande_id'], not the (possibly
        NumeroCommande) commande_id passed by the LLM."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx({"ar_commande_id": 7})
        lookup = MagicMock(return_value="conforme")

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", lookup), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}):
            scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr", subject="x", body="y",
                commande_id="CF999", tool_context=ctx,
            )

        args, _ = lookup.call_args
        assert args[1] == 7, f"expected lookup on state Id 7, got {args}"

    def test_fallback_param_commande_id_when_no_state(self):
        """Degraded path: with no tool_context (no state), the lookup falls
        back to the LLM-passed commande_id — which may be a NumeroCommande
        (e.g. 'CF101173'), not the Id. The SELECT WHERE Id then matches
        nothing and returns None → NO cc is added (fail-safe: never a wrong
        CC, just a missing one in this rare unstated path)."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        lookup = MagicMock(return_value=None)

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", lookup), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send:
            scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr", subject="x", body="y",
                commande_id="CF101173", tool_context=None,
            )

        # Fallback uses the param commande_id verbatim ("CF101173")
        args, _ = lookup.call_args
        assert args[1] == "CF101173"
        # Lookup returns None (Id mismatch) → no cc added
        _, kwargs = send.call_args
        assert kwargs.get("cc") is None

    def test_dry_run_never_adds_cc(self):
        """Dry-run sends to the dev address but NEVER adds the escalation CC,
        even when the AR is non_conforme."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "true"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", return_value="non_conforme"), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr", subject="x", body="y",
                commande_id="5", tool_context=ctx,
            )

        assert result["mode"] == "dry_run"
        send.assert_called_once()
        _, kwargs = send.call_args
        assert kwargs.get("cc") is None
        assert kwargs.get("to") == "david.gnaglo@thaink2.com"

    def test_dry_run_redirects_to_dev_address(self):
        """Dry-run redirects to the dev address (david.gnaglo@thaink2.com)
        — the audit row records that redirected recipient."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()
        captured = {}

        def fake_audit(engine, **kw):
            captured.update(kw)
            return 42

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "true"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_log_audit", side_effect=fake_audit), \
             patch.object(scei_mail, "_send_outlook"):
            scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr", subject="x", body="y",
                commande_id="5", tool_context=ctx,
            )

        assert captured.get("sent_to") == "david.gnaglo@thaink2.com", (
            f"dry-run must redirect to the dev address, got {captured.get('sent_to')}"
        )

    def test_live_audit_failure_refuses_send(self):
        """Fix L: if the audit row cannot be persisted (DB plantée, GRANT
        manquant), the live send is refused — enforces 'no audit = no
        send' guarantee documented in the module header."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, state = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_log_audit", return_value=None), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="x",
                body="y",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert result["success"] is False
        assert result["mode"] == "live"
        assert result["reason"] == "audit_log_failed"
        send.assert_not_called()
        # Counter NOT incremented on a refused send
        assert state.get("scei_mail_sent_count", 0) == 0

    def test_dry_run_tolerates_audit_failure(self):
        """In dry-run, an audit failure is logged but the dry-run still
        completes and the review mail is still sent to the dev address
        (audit is informational here, not a live security gate)."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "true"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_log_audit", return_value=None), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="x",
                body="y",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert result["success"] is True
        assert result["mode"] == "dry_run"
        send.assert_called_once()

    def test_audit_log_written_before_send_dry_run(self):
        """Audit row must be inserted before any send attempt."""
        from th2customers.scei.tools import scei_mail

        engine = _mock_engine_with_whitelist(["acheteur@scei88.fr"])
        ctx, _ = _ctx()

        call_order = []

        def fake_log(*a, **kw):
            call_order.append("audit")
            return 42

        def fake_send(*a, **kw):
            call_order.append("send")
            return {"ok": True}

        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_log_audit", side_effect=fake_log), \
             patch.object(scei_mail, "_send_outlook", side_effect=fake_send):
            scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="x",
                body="y",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert call_order == ["audit", "send"], (
            f"audit must precede send, got {call_order}"
        )

    def test_missing_db_credentials_fails_safely(self):
        """When DB env vars are missing, refuse rather than send blindly."""
        from th2customers.scei.tools import scei_mail

        ctx, _ = _ctx()

        with patch.object(scei_mail, "_get_db_engine", return_value=None), \
             patch.object(scei_mail, "_send_outlook") as send:
            result = scei_mail.tool_send_scei_mail(
                to="acheteur@scei88.fr",
                subject="x",
                body="y",
                commande_id="CMD-1",
                tool_context=ctx,
            )

        assert result["success"] is False
        assert result["reason"] in ("db_unavailable", "recipient_not_whitelisted")
        send.assert_not_called()


# ---------------------------------------------------------------------------
# outlook_mail._cc_recipients — multi-CC support
# ---------------------------------------------------------------------------


class TestCcRecipients:
    def test_single_address(self):
        from th2agent.tools_store.portfolio.outlook_mail import _cc_recipients
        assert _cc_recipients("a@x.fr") == [
            {"emailAddress": {"address": "a@x.fr"}}
        ]

    def test_multiple_csv(self):
        from th2agent.tools_store.portfolio.outlook_mail import _cc_recipients
        assert _cc_recipients("a@x.fr, b@y.fr") == [
            {"emailAddress": {"address": "a@x.fr"}},
            {"emailAddress": {"address": "b@y.fr"}},
        ]

    def test_none_and_blank(self):
        from th2agent.tools_store.portfolio.outlook_mail import _cc_recipients
        assert _cc_recipients(None) == []
        assert _cc_recipients("") == []
        assert _cc_recipients(" , ") == []


class TestInterimRedirectAndConforme:
    def test_redirect_forces_central_address(self):
        """SCEI_BUYER_NOTIF_REDIRECT → buyer notif goes to the central SCEI
        address, not the per-buyer email; non_conforme still CCs escalation."""
        from th2customers.scei.tools import scei_mail
        engine = _mock_engine_with_whitelist([])  # buyer NOT in DB whitelist
        ctx, _ = _ctx()
        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false",
                                       "SCEI_BUYER_NOTIF_REDIRECT": "com@scei88.fr"}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", return_value="non_conforme"), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send:
            result = scei_mail.tool_send_scei_mail(
                to="dorian.jacquot@scei88.fr", subject="x", body="y",
                commande_id="5", tool_context=ctx)
        assert result["success"] is True
        _, kwargs = send.call_args
        assert kwargs.get("to") == "com@scei88.fr"
        assert kwargs.get("cc") == "david.gnaglo@thaink2.com,c.roussel@scei88.fr,j.fruchart@scei88.fr"

    def test_redirect_address_allowed_without_db(self):
        from th2customers.scei.tools import scei_mail
        engine = _mock_engine_with_whitelist([])
        with patch.dict("os.environ", {"SCEI_BUYER_NOTIF_REDIRECT": "com@scei88.fr"}):
            assert scei_mail._validate_recipient("com@scei88.fr", engine) is True
            assert scei_mail._validate_recipient("random@x.fr", engine) is False

    def test_no_redirect_keeps_nominal(self):
        """Without the env var, the per-buyer address is kept (must be whitelisted)."""
        from th2customers.scei.tools import scei_mail
        engine = _mock_engine_with_whitelist(["dorian.jacquot@scei88.fr"])
        ctx, _ = _ctx()
        with patch.dict("os.environ", {"SCEI_MAIL_AUTO_DRY_RUN": "false",
                                       "SCEI_BUYER_NOTIF_REDIRECT": ""}), \
             patch.object(scei_mail, "_get_db_engine", return_value=engine), \
             patch.object(scei_mail, "_lookup_statut_global", return_value="non_conforme"), \
             patch.object(scei_mail, "_send_outlook", return_value={"ok": True}) as send:
            scei_mail.tool_send_scei_mail(to="dorian.jacquot@scei88.fr", subject="x",
                body="y", commande_id="5", tool_context=ctx)
        _, kwargs = send.call_args
        assert kwargs.get("to") == "dorian.jacquot@scei88.fr"
