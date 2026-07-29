"""Anti-regression: webhook ADK session must be one-per-AR.

Live incident 2026-05-19: every Outlook webhook event reused the same
ADK session keyed on ``sub_db_id`` (``session_id="webhook_1"`` for the
SCEI subscription). After a few days the session had accumulated
3656+ events and the next agent run crashed with
``litellm.ContextWindowExceededError`` once Gemini's 1M-token window
was breached.

The fix pins ``session_id`` to ``log_id`` so each webhook event gets
its own conversation — bounded history, no cross-AR pollution.

This test reads the source instead of mocking the full webhook
pipeline (DB session, Outlook fetch, agent run, SSE notify) because
the property we care about is a small invariant on one line of code,
and a source-level pin is both cheaper and harder to silently
regress.
"""

from __future__ import annotations

import pathlib


def test_outlook_session_id_uses_log_id_not_sub_db_id() -> None:
    src_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "th2agent"
        / "routers"
        / "webhook_handlers"
        / "outlook.py"
    )
    text = src_path.read_text(encoding="utf-8")

    assert 'session_id = f"webhook_{log_id}"' in text, (
        "Outlook webhook handler must build session_id from log_id "
        "(one ADK session per webhook event). Regression would re-open "
        "the ContextWindowExceeded bug of 2026-05-19."
    )
    assert 'webhook_{sub_db_id}' not in text, (
        "Found legacy 'webhook_{sub_db_id}' pattern: every AR would "
        "share a single ADK session and accumulate unbounded history."
    )


def test_outlook_push_notification_links_to_webhooks_tab() -> None:
    """Webhook runs surface in /webhooks (Activity tab), not in /chat.

    Decision 2026-05-19 (live): the operator's chat sidebar must not be
    polluted by automated webhook conversations. They have a dedicated
    tab in the Webhooks section. The push notification link must point
    there so clicking it lands the operator on the right page.
    """
    src_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / 'src'
        / 'th2agent'
        / 'routers'
        / 'webhook_handlers'
        / 'outlook.py'
    )
    text = src_path.read_text(encoding='utf-8')

    assert 'link=f"/webhooks?log={log_id}",' in text, (
        'Push notification link must target /webhooks?log=<log_id> so '
        'the operator lands on the Webhooks Activity tab with the right '
        'row expanded — not in the agent chat.'
    )
    assert '/chat?agent=' not in text, (
        'Legacy push notification pointed to /chat?agent=...&session=...; '
        'this exposed webhook conversations in the chat sidebar (forbidden).'
    )
