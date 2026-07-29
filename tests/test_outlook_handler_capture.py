"""Unit tests for the email body + attachments capture in the
``outlook`` webhook handler.

We exercise the two pure helpers (``_strip_html`` and the
``_PlainTextExtractor``) directly. The end-to-end orchestration in
``process_webhook_log_row`` is integration-tested elsewhere; mocking
the full chain (DB session, Graph fetch, ADK run, SSE notify) for one
PR adds more test code than it protects.
"""
from __future__ import annotations

from th2agent.routers.webhook_handlers import outlook as h


class TestStripHtml:
    def test_returns_empty_on_empty_input(self):
        assert h._strip_html("") == ""
        assert h._strip_html(None) == ""  # type: ignore[arg-type]

    def test_extracts_visible_text(self):
        html = "<p>Hello <b>world</b></p>"
        assert "Hello" in h._strip_html(html)
        assert "world" in h._strip_html(html)

    def test_ignores_script_and_style(self):
        html = (
            "<html><head>"
            "<style>body { color: red; }</style>"
            "<script>alert('xss')</script>"
            "</head><body><p>visible</p></body></html>"
        )
        out = h._strip_html(html)
        assert "visible" in out
        assert "alert" not in out
        assert "color" not in out

    def test_does_not_crash_on_malformed_html(self):
        # html.parser is permissive but make sure we swallow whatever
        # remains so a deformed email cannot kill the worker.
        assert h._strip_html("<p>broken<br") == "broken"

    def test_strips_inline_event_handlers_indirectly(self):
        # We only strip *content*, not attributes; but the visible text
        # never includes attribute values, which is what matters for
        # the searchable plain-text column.
        html = '<p onclick="evil()">Click me</p>'
        out = h._strip_html(html)
        assert "Click me" in out
        assert "evil" not in out
