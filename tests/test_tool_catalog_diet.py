"""Tool catalogue hygiene: unique Python names, legacy aliases, lean replies."""
from __future__ import annotations

import base64
import importlib
import logging
from unittest.mock import MagicMock, patch

import pytest

from apowerb.tools_store.tools_helpers import (
    _LEGACY_TOOL_RENAMES,
    load_agent_tools_functions,
    tool_manager,
)

P = "apowerb.tools_store.portfolio."


def test_no_tool_function_name_is_shared_between_modules():
    by_name: dict[str, dict[int, str]] = {}
    for category, tools in tool_manager.get_all_tools().items():
        if category == "overlay":
            continue
        module = importlib.import_module(P + category)
        for qualified in tools:
            name = qualified.split(".", 1)[1]
            fn = getattr(module, name)
            if callable(fn) and name.startswith("tool_"):
                # Same object re-exported in another module is not a clash.
                by_name.setdefault(name, {})[id(fn)] = qualified
    clashes = {n: sorted(v.values()) for n, v in by_name.items() if len(v) > 1}
    assert not clashes, clashes


@pytest.mark.parametrize("old,new", [
    ("google_drive.tool_list_files", "google_drive.tool_gdrive_list_files"),
    ("google_drive.tool_search_files", "google_drive.tool_gdrive_search_files"),
    ("google_drive.tool_download_file", "google_drive.tool_gdrive_download_file"),
    ("google_gmail.tool_list_emails", "google_gmail.tool_gmail_list_emails"),
    ("google_gmail.tool_read_email", "google_gmail.tool_gmail_read_email"),
    ("google_gmail.tool_search_emails", "google_gmail.tool_gmail_search_emails"),
    ("github.tool_read_file", "github.tool_github_read_file"),
])
def test_old_qualified_name_resolves_to_the_new_one(old, new, caplog):
    caplog.set_level(logging.WARNING, logger="apowerb.tools_store.tools_helpers")
    assert _LEGACY_TOOL_RENAMES[old] == new
    names, funcs = load_agent_tools_functions([old], owner_id="u@example.com")
    assert names == [new]
    assert funcs[0].__name__ == new.split(".")[1]


def test_onedrive_and_outlook_keep_their_names():
    names, _ = load_agent_tools_functions(
        ["onedrive_read.tool_list_files", "outlook_mail.tool_read_email"],
        owner_id="u@example.com",
    )
    assert names == ["onedrive_read.tool_list_files", "outlook_mail.tool_read_email"]


@pytest.mark.parametrize("dead", [
    "marketing.tool_marketing",
    "audio.tool_generate_audio",
    "api_call.tool_api_call",
])
def test_removed_tool_is_gone_and_ignored_by_the_resolver(dead):
    module, name = dead.split(".")
    assert not hasattr(importlib.import_module(P + module), name)
    names, funcs = load_agent_tools_functions(
        [dead, "marketing.tool_hubspot_get_sales_leads"], owner_id="u@example.com"
    )
    assert names == ["marketing.tool_hubspot_get_sales_leads"]
    assert len(funcs) == 1


# ------------------------------------------------------------------ replies


def test_html_to_text_drops_style_script_and_decodes_entities():
    from apowerb.helpers.email_text import MAX_BODY_CHARS, cap_body, html_to_text

    raw = (
        "<html><head><style>p{color:red}</style><script>alert(1)</script></head>"
        "<body><p>Caf&eacute; &amp; cr&egrave;me&nbsp;!</p></body></html>"
    )
    assert html_to_text(raw) == "Café & crème\xa0!"
    text, cut = cap_body("x" * (MAX_BODY_CHARS + 1))
    assert len(text) == MAX_BODY_CHARS and cut is True
    assert cap_body("ok") == ("ok", False)


def test_outlook_read_email_cleans_and_caps_body():
    from apowerb.tools_store.portfolio import outlook_mail

    html = "<style>a{}</style><p>Tom &amp; Jerry</p>" + "z" * 20000
    msg = {"id": "1", "subject": "s", "body": {"contentType": "html", "content": html},
           "toRecipients": [], "ccRecipients": [], "hasAttachments": False}
    resp = MagicMock(status_code=200)
    resp.json.return_value = msg
    with patch.object(outlook_mail, "_graph_headers", return_value={}), \
            patch.object(outlook_mail.httpx, "get", return_value=resp):
        out = outlook_mail.tool_read_email("1")
    assert out["body"].startswith("Tom & Jerry")
    assert "a{}" not in out["body"]
    assert len(out["body"]) == 15000 and out["body_truncated"] is True


def test_gmail_read_email_cleans_and_caps_body():
    from apowerb.tools_store.portfolio import google_gmail

    html = "<style>a{}</style><p>Tom &amp; Jerry</p>" + "z" * 20000
    data = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
    msg = {"id": "1", "payload": {"mimeType": "text/html", "headers": [], "body": {"data": data}}}
    resp = MagicMock(status_code=200)
    resp.json.return_value = msg
    with patch.object(google_gmail, "google_auth_headers", return_value={}), \
            patch.object(google_gmail.httpx, "get", return_value=resp):
        out = google_gmail.tool_gmail_read_email("1")
    assert out["body"].startswith("Tom & Jerry")
    assert "a{}" not in out["body"]
    assert len(out["body"]) == 15000 and out["body_truncated"] is True


@pytest.mark.parametrize("mime,export,fail_marker", [
    ("application/vnd.google-apps.document", True, "Export failed"),
    ("text/plain", False, "Download failed"),
])
def test_drive_read_failure_is_an_error_not_a_success(mime, export, fail_marker):
    from apowerb.tools_store.portfolio import google_drive

    meta = MagicMock(status_code=200)
    meta.json.return_value = {"id": "f", "name": "n", "mimeType": mime, "webViewLink": "http://v"}
    denied = MagicMock(status_code=403, text="forbidden")
    with patch.object(google_drive, "google_auth_headers", return_value={}), \
            patch.object(google_drive.httpx, "get", side_effect=[meta, denied]):
        out = google_drive.tool_read_drive_file("f")
    assert out["status"] == "error"
    assert fail_marker in out["message"] and "403" in out["message"]
    assert out["webViewLink"] == "http://v"


def test_onedrive_metadata_has_no_download_url_and_list_note_only_when_empty():
    from apowerb.tools_store.portfolio import onedrive_read

    item = {"id": "i", "name": "a.txt", "file": {"mimeType": "text/plain"},
            "@microsoft.graph.downloadUrl": "https://secret"}
    resp = MagicMock(status_code=200)
    resp.json.return_value = item
    with patch.object(onedrive_read, "_graph_headers", return_value={}), \
            patch.object(onedrive_read.httpx, "get", return_value=resp) as get:
        out = onedrive_read.tool_get_file_metadata(item_id="i")
    assert "downloadUrl" not in out
    assert "downloadUrl" not in get.call_args.kwargs["params"]["$select"]

    for value, expect_note in (([item], False), ([], True)):
        resp.json.return_value = {"value": value}
        with patch.object(onedrive_read, "_graph_headers", return_value={}), \
                patch.object(onedrive_read.httpx, "get", return_value=resp):
            out = onedrive_read.tool_list_files()
        assert out["status"] == "success"
        assert ("note" in out) is expect_note


def test_hubspot_leads_lean_signature_and_no_raw():
    import inspect

    from apowerb.tools_store.portfolio import marketing

    sig = inspect.signature(marketing.tool_hubspot_get_sales_leads)
    assert sig.parameters["limit"].default == 25
    assert "after_date" not in sig.parameters and "filters" not in sig.parameters
    resp = MagicMock()
    resp.json.return_value = {
        "results": [{"id": "1", "properties": {"email": "a@b.c", "hs_score": "42"}}]
    }
    with patch.object(marketing.requests, "get", return_value=resp) as get:
        out = marketing.tool_hubspot_get_sales_leads(api_key="k", properties=["email", "hs_score"])
    assert get.call_args.kwargs["params"]["limit"] == 25
    assert out["leads"][0]["email"] == "a@b.c"
    assert out["leads"][0]["hs_score"] == "42"  # requested custom property is returned
    assert "raw" not in out["leads"][0]


def test_generated_media_replies_carry_no_base64(tmp_path, monkeypatch):
    from apowerb.tools_store.portfolio import audio, image_generation

    monkeypatch.setenv("ROOT_AGENT_ID", "t1")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(audio, "agent_upload_dir", lambda _id: tmp_path)
    monkeypatch.setattr(image_generation, "agent_upload_dir", lambda _id: tmp_path)
    tts = ("OpenAI TTS", lambda *a, **k: (b"ID3audio", "audio/mpeg"), "OPENAI_API_KEY")
    monkeypatch.setitem(audio._TTS_PROVIDERS, "openai", tts)
    out = audio.tool_text_to_speech("hello", provider="openai")
    assert out["status"] == "success" and "base64_data" not in out
    assert out["download_path"]

    gen = ("Fake", lambda *a, **k: (b"\x89PNGdata", "image/png"), "OPENAI_API_KEY")
    monkeypatch.setitem(image_generation._PROVIDERS, "openai", gen)
    out = image_generation.tool_generate_image("a cat", provider="openai")
    assert out["status"] == "success" and "base64_data" not in out
    assert out["download_path"]


def test_analyze_audio_truncates_transcript_only_with_summary(tmp_path, monkeypatch):
    from apowerb.tools_store.portfolio import audio

    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    long_text = "w" * 6000
    monkeypatch.setattr(
        audio, "tool_speech_to_text",
        lambda *a, **k: {"status": "success", "transcription": long_text},
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = audio.tool_analyze_audio(str(f))
    assert out["transcription"] == long_text and "transcription_truncated" not in out

    fake_genai = MagicMock()
    fake_genai.Client.return_value.models.generate_content.return_value.text = "Summary: ok"
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr("google.genai", fake_genai)
    out = audio.tool_analyze_audio(str(f))
    assert out["summary"] == "Summary: ok"
    assert len(out["transcription"]) == 4000
    assert out["transcription_truncated"] is True


def test_workflow_resolves_a_legacy_tool_name(monkeypatch):
    from apowerb.core import workflow_runtime
    from apowerb.tools_store import tools_helpers

    def fake_load(tools, owner_id):
        return ["google_drive.tool_gdrive_list_files"], [lambda: "ok"]

    monkeypatch.setattr(tools_helpers, "load_agent_tools_functions", fake_load)
    func = workflow_runtime.resolve_tool("tool_config1:tool_list_files", "a@b.c")
    assert func() == "ok"


def test_dynamics_record_id_prefers_the_entity_key():
    from apowerb.tools_store.portfolio.dynamics365 import _record_id

    gid = "11111111-2222-3333-4444-555555555555"
    other = "99999999-2222-3333-4444-555555555555"
    row = {"parentcustomerid": other, "contactid": gid}
    assert _record_id(row, "contacts") == gid
    assert _record_id({"opportunityid": gid, "customerid": other}, "opportunities") == gid
    assert _record_id({"parentcustomerid": other, "contactid": gid}) is None
    assert _record_id({"leadid": gid}) == gid


def test_memory_clip_says_the_text_was_cut():
    from apowerb.tools_store.portfolio.memory import _MAX_FIELD_CHARS, _clip

    long = "x" * (_MAX_FIELD_CHARS + 50)
    out = _clip(long)
    assert out.startswith("x" * (_MAX_FIELD_CHARS - 50))
    assert out.endswith(f"[truncated, {len(long)} chars total]")
    assert len(out) <= _MAX_FIELD_CHARS + 3
    assert _clip("short") == "short" and _clip(3) == 3

