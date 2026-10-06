"""Tests for the Notion tools.

Request shapes are pinned against the contract verified 2026-10-06:
``api.notion.com/v1``, ``Authorization: Bearer``, ``Notion-Version: 2026-03-11``,
databases queried through their data source.
httpx is mocked throughout — no network, no real Notion workspace.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apowerb.tools_store.portfolio import notion

PAGE_ID = "1429989fe8ac4effbc8f57f56486db54"
DB_ID = "248104cd477e80fdb757e945d38000bd"
DS_ID = "248104cd-477e-80af-bc30-000bd28de8f9"


def _resp(payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload or {}
    resp.raise_for_status.return_value = None
    resp.text = ""
    return resp


def _http_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def _rt(text: str) -> list[dict]:
    return [{"type": "text", "plain_text": text}]


def _page(pid: str = PAGE_ID, title: str = "Roadmap", **props) -> dict:
    properties = {"Name": {"type": "title", "title": _rt(title)}}
    properties.update(props)
    return {
        "object": "page",
        "id": pid,
        "url": f"https://www.notion.so/{pid}",
        "properties": properties,
    }


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("NOTION_API_TOKEN", "secret_tok")


class TestConfig:
    def test_missing_token(self, monkeypatch):
        monkeypatch.delenv("NOTION_API_TOKEN", raising=False)
        with patch("httpx.request") as req:
            result = notion.tool_notion_search(query="x")
        assert result["status"] == "error"
        assert "NOTION_API_TOKEN" in result["error_message"]
        req.assert_not_called()

    def test_headers(self, token):
        with patch("httpx.request", return_value=_resp({"results": []})) as req:
            notion.tool_notion_search(query="x")
        headers = req.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret_tok"
        assert headers["Notion-Version"] == "2026-03-11"

    @pytest.mark.parametrize(
        "raw",
        [
            PAGE_ID,
            "1429989f-e8ac-4eff-bc8f-57f56486db54",
            f"https://www.notion.so/acme/Roadmap-{PAGE_ID}?pvs=4",
            f"https://www.notion.so/acme/Roadmap-{PAGE_ID}?v={DB_ID}",
        ],
    )
    def test_id_from_url_or_uuid(self, raw):
        assert notion._notion_id(raw, "page_id") == PAGE_ID

    def test_invalid_id(self, token):
        with patch("httpx.request") as req:
            result = notion.tool_notion_read_page("not-an-id")
        assert result["status"] == "error"
        req.assert_not_called()


class TestSearch:
    def test_request_shape_and_summary(self, token):
        payload = {
            "results": [
                _page(),
                {"object": "data_source", "id": DS_ID, "title": _rt("CRM")},
            ],
            "has_more": True,
        }
        with patch("httpx.request", return_value=_resp(payload)) as req:
            result = notion.tool_notion_search(query=" road ", object_type="database")
        assert req.call_args.args[:2] == ("POST", "https://api.notion.com/v1/search")
        assert req.call_args.kwargs["json"] == {
            "page_size": 10,
            "query": "road",
            "filter": {"property": "object", "value": "data_source"},
        }
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["has_more"] is True
        assert result["results"][0]["title"] == "Roadmap"
        assert result["results"][1]["title"] == "CRM"

    def test_bad_object_type(self, token):
        with patch("httpx.request") as req:
            result = notion.tool_notion_search(object_type="block")
        assert result["status"] == "error"
        req.assert_not_called()


class TestReadPage:
    def test_reads_properties_and_paginated_blocks(self, token):
        blocks_1 = {
            "results": [
                {"type": "heading_1", "heading_1": {"rich_text": _rt("Intro")}},
                {"type": "paragraph", "paragraph": {"rich_text": _rt("Hello")}},
            ],
            "has_more": True,
            "next_cursor": "c2",
        }
        blocks_2 = {
            "results": [
                {"type": "to_do", "to_do": {"rich_text": _rt("Ship"), "checked": True}}
            ],
            "has_more": False,
        }
        page = _page(Status={"type": "status", "status": {"name": "Done"}})
        with patch(
            "httpx.request", side_effect=[_resp(page), _resp(blocks_1), _resp(blocks_2)]
        ) as req:
            result = notion.tool_notion_read_page(f"https://notion.so/x-{PAGE_ID}")
        assert result["status"] == "success"
        assert result["title"] == "Roadmap"
        assert result["properties"]["Status"] == "Done"
        assert result["content"] == "# Intro\nHello\n[x] Ship"
        assert result["truncated"] is False
        calls = req.call_args_list
        assert calls[0].args[1] == f"https://api.notion.com/v1/pages/{PAGE_ID}"
        assert (
            calls[1].args[1] == f"https://api.notion.com/v1/blocks/{PAGE_ID}/children"
        )
        assert calls[2].kwargs["params"]["start_cursor"] == "c2"

    def test_truncates_at_max_blocks(self, token):
        blocks = {
            "results": [
                {"type": "paragraph", "paragraph": {"rich_text": _rt(str(i))}}
                for i in range(5)
            ],
            "has_more": False,
        }
        with patch("httpx.request", side_effect=[_resp(_page()), _resp(blocks)]):
            result = notion.tool_notion_read_page(PAGE_ID, max_blocks=2)
        assert result["content"] == "0\n1"
        assert result["truncated"] is True

    def test_has_more_without_cursor_stops(self, token):
        blocks = {
            "results": [{"type": "paragraph", "paragraph": {"rich_text": _rt("x")}}],
            "has_more": True,
            "next_cursor": None,
        }
        with patch("httpx.request", side_effect=[_resp(_page()), _resp(blocks)]) as req:
            result = notion.tool_notion_read_page(PAGE_ID)
        assert result["content"] == "x"
        assert result["truncated"] is True
        assert req.call_count == 2

    def test_404_hints_at_sharing(self, token):
        with patch("httpx.request", side_effect=_http_error(404, "object_not_found")):
            result = notion.tool_notion_read_page(PAGE_ID)
        assert result["status"] == "error"
        assert result["http_status"] == 404
        assert "shared with the integration" in result["error_message"]


class TestQueryDatabase:
    def test_resolves_data_source_and_paginates(self, token):
        database = {
            "object": "database",
            "data_sources": [{"id": DS_ID, "name": "CRM"}],
        }
        q1 = {"results": [_page(title="A")], "has_more": True, "next_cursor": "n"}
        q2 = {"results": [_page(title="B")], "has_more": False, "next_cursor": None}
        flt = {"property": "Status", "status": {"equals": "Done"}}
        with patch(
            "httpx.request", side_effect=[_resp(database), _resp(q1), _resp(q2)]
        ) as req:
            result = notion.tool_notion_query_database(database_id=DB_ID, filter=flt)
        assert result["status"] == "success"
        assert [r["title"] for r in result["results"]] == ["A", "B"]
        assert result["has_more"] is False
        calls = req.call_args_list
        assert calls[0].args == ("GET", f"https://api.notion.com/v1/databases/{DB_ID}")
        assert calls[1].args == (
            "POST",
            f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
        )
        assert calls[1].kwargs["json"]["filter"] == flt
        assert calls[2].kwargs["json"]["start_cursor"] == "n"

    def test_limit_caps_rows(self, token):
        q = {
            "results": [_page(title="A"), _page(title="B")],
            "has_more": True,
            "next_cursor": "n",
        }
        with patch("httpx.request", return_value=_resp(q)) as req:
            result = notion.tool_notion_query_database(data_source_id=DS_ID, limit=2)
        assert result["count"] == 2
        assert result["has_more"] is True
        assert req.call_count == 1
        assert req.call_args.kwargs["json"]["page_size"] == 2

    def test_several_data_sources_is_an_error(self, token):
        database = {
            "data_sources": [{"id": DS_ID, "name": "A"}, {"id": DB_ID, "name": "B"}]
        }
        with patch("httpx.request", return_value=_resp(database)) as req:
            result = notion.tool_notion_query_database(database_id=DB_ID)
        assert result["status"] == "error"
        assert "data_source_id" in result["error_message"]
        assert req.call_count == 1

    def test_requires_an_id(self, token):
        with patch("httpx.request") as req:
            result = notion.tool_notion_query_database()
        assert result["status"] == "error"
        req.assert_not_called()


class TestCreatePage:
    def test_under_page(self, token):
        created = {"id": "new", "url": "https://notion.so/new"}
        with patch("httpx.request", return_value=_resp(created)) as req:
            result = notion.tool_notion_create_page(
                title="Notes", parent_page_id=PAGE_ID, content="One\n\nTwo"
            )
        assert result == {
            "status": "success",
            "id": "new",
            "url": "https://notion.so/new",
        }
        body = req.call_args.kwargs["json"]
        assert req.call_args.args == ("POST", "https://api.notion.com/v1/pages")
        assert body["parent"] == {"type": "page_id", "page_id": PAGE_ID}
        assert body["properties"]["title"]["title"][0]["text"]["content"] == "Notes"
        texts = [
            b["paragraph"]["rich_text"][0]["text"]["content"] for b in body["children"]
        ]
        assert texts == ["One", "Two"]

    def test_database_row_uses_title_property(self, token):
        schema = {
            "properties": {"Deal": {"type": "title"}, "Stage": {"type": "select"}}
        }
        created = {"id": "row", "url": "u"}
        with patch("httpx.request", side_effect=[_resp(schema), _resp(created)]) as req:
            result = notion.tool_notion_create_page(
                title="Acme",
                data_source_id=DS_ID,
                properties={"Stage": {"select": {"name": "Won"}}},
            )
        assert result["status"] == "success"
        body = req.call_args_list[1].kwargs["json"]
        assert body["parent"]["type"] == "data_source_id"
        assert body["properties"]["Deal"]["title"][0]["text"]["content"] == "Acme"
        assert body["properties"]["Stage"] == {"select": {"name": "Won"}}
        assert "children" not in body

    def test_long_paragraph_is_split(self, token):
        with patch("httpx.request", return_value=_resp({"id": "x"})) as req:
            notion.tool_notion_create_page(
                title="T", parent_page_id=PAGE_ID, content="a" * 4500
            )
        children = req.call_args.kwargs["json"]["children"]
        lengths = [
            len(b["paragraph"]["rich_text"][0]["text"]["content"]) for b in children
        ]
        assert lengths == [2000, 2000, 500]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"title": "T"},
            {"title": "T", "parent_page_id": PAGE_ID, "database_id": DB_ID},
            {"title": " ", "parent_page_id": PAGE_ID},
            {"title": "T", "parent_page_id": PAGE_ID, "properties": {"X": {}}},
        ],
    )
    def test_invalid_arguments(self, token, kwargs):
        with patch("httpx.request") as req:
            result = notion.tool_notion_create_page(**kwargs)
        assert result["status"] == "error"
        req.assert_not_called()

    def test_auth_error_mapped(self, token):
        with patch("httpx.request", side_effect=_http_error(401, "unauthorized")):
            result = notion.tool_notion_create_page(title="T", parent_page_id=PAGE_ID)
        assert result["http_status"] == 401
        assert "NOTION_API_TOKEN" in result["error_message"]
