"""Model-facing response shape of the Dynamics 365 tools."""

from __future__ import annotations

from unittest.mock import patch

from apowerb.tools_store.portfolio import dynamics365 as d365

_GUID = "11111111-2222-3333-4444-555555555555"


def test_query_defaults_to_25_rows():
    with patch.object(d365, "_request", return_value={"value": []}) as req:
        d365.tool_dynamics365_query("accounts")
    assert req.call_args.kwargs["params"]["$top"] == 26


def test_clean_keeps_lookup_guids_next_to_their_display_value():
    row = {
        "@odata.etag": "x",
        "_owner_value": _GUID,
        f"_owner_value{d365._FORMATTED_SUFFIX}": "Jane",
        "_parent_value": _GUID,
        "name": "ACME",
    }
    out = d365._clean(row)
    assert out["_owner_value"] == _GUID  # needed for filters and @odata.bind
    assert out[f"_owner_value{d365._FORMATTED_SUFFIX}"] == "Jane"
    assert "@odata.etag" not in out
    assert out["_parent_value"] == _GUID
    assert out["name"] == "ACME"


def test_create_returns_id_not_the_row():
    data = {"@odata.etag": "x", "accountid": _GUID, "name": "ACME", "description": "long"}
    with patch.object(d365, "_request", return_value=data):
        out = d365.tool_dynamics365_create_record("accounts", {"name": "ACME"})
    assert out == {"status": "success", "id": _GUID}
