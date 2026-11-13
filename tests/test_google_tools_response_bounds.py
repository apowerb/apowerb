"""Bounds and error mapping of the Google Docs / Sheets / Calendar tools."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from apowerb.tools_store.portfolio import google_calendar, google_docs, google_sheets
from apowerb.tools_store.portfolio.http_errors import google_error_result


def _resp(json_body, status=200):
    req = httpx.Request("GET", "https://example.test/x")
    return httpx.Response(status, json=json_body, request=req)


def test_docs_content_capped():
    doc = {"title": "t", "body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": "a" * 40000}}]}}]}}
    with patch.object(google_docs, "google_auth_headers", return_value={}), patch.object(
        google_docs.httpx, "get", return_value=_resp(doc)
    ):
        out = google_docs.tool_read_document("d")
    assert len(out["content"]) == 30000
    assert out["total_chars"] == 40000 and out["truncated"] is True


def test_sheet_capped_but_table_source_is_not():
    values = [["h"]] + [[str(i)] for i in range(499)]
    with patch.object(google_sheets, "google_auth_headers", return_value={}), patch.object(
        google_sheets.httpx, "get", return_value=_resp({"range": "S", "values": values})
    ):
        agent = google_sheets.tool_read_sheet("s")
        table = google_sheets.tool_read_sheet_table("s")
    assert len(agent["values"]) == 200 and agent["total_rows"] == 500 and agent["truncated"] is True
    assert len(table["rows"]) == 499


def test_calendar_clamp_description_and_no_link():
    ev = {"id": "1", "summary": "s", "description": "d" * 1000, "htmlLink": "http://x"}
    with patch.object(google_calendar, "google_auth_headers", return_value={}), patch.object(
        google_calendar.httpx, "get", return_value=_resp({"items": [ev]})
    ) as get:
        out = google_calendar.tool_list_events(max_results=9999)
    assert get.call_args.kwargs["params"]["maxResults"] == 50
    event = out["events"][0]
    assert len(event["description"]) == 303 and "htmlLink" not in event


def test_google_error_gives_hint_and_google_detail():
    resp = _resp({"error": {"message": "Unable to parse range: Foo"}}, status=400)
    exc = httpx.HTTPStatusError("bad", request=resp.request, response=resp)
    out = google_error_result(exc, "Google Sheets")
    assert out["http_status"] == 400 and out["retry"] is False
    assert "Invalid request" in out["message"] and "Unable to parse range" in out["message"]


def test_google_error_5xx_is_retryable_and_timeout():
    resp = _resp({}, status=503)
    exc = httpx.HTTPStatusError("x", request=resp.request, response=resp)
    assert google_error_result(exc, "Google Docs")["retry"] is True
    assert google_error_result(httpx.ReadTimeout("t"), "Google Docs")["retry"] is True
