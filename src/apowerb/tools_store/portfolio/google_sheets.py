"""Google Sheets tools -- Read and write spreadsheet data via Google Sheets API.

Provides 3 tools for reading cell ranges, writing data, and retrieving
spreadsheet metadata from Google Sheets.

Auth credentials are injected as environment variables by the tool_config
system at agent runtime:
  - ``GOOGLE_SHEETS_REFRESH_TOKEN`` -- OAuth2 refresh token

The shared helper ``google_auth_headers()`` transparently exchanges the
refresh token for a short-lived access token.
"""

import json
from logging import getLogger

import httpx

from apowerb.tools_store.portfolio.google_auth import google_auth_headers
from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError

logger = getLogger(__name__)

_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_SERVICE = "GOOGLE_SHEETS"


def tool_read_sheet(spreadsheet_id: str, range: str = "Sheet1") -> dict:
    """Read cell values from a Google Sheets range.

    Args:
        spreadsheet_id: The ID of the spreadsheet (from the URL).
        range: A1 notation range to read (e.g. 'Sheet1', 'Sheet1!A1:D10').
            Defaults to 'Sheet1' (entire first sheet).

    Returns:
        A dict with status, range, values (2D list), total_rows, and total_cols.
    """
    try:
        headers = google_auth_headers(_SERVICE)
        resp = httpx.get(
            f"{_BASE}/{spreadsheet_id}/values/{range}",
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        values = data.get("values", [])
        return {
            "status": "ok",
            "range": data.get("range"),
            "values": values,
            "total_rows": len(values),
            "total_cols": max((len(row) for row in values), default=0),
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_read_sheet failed")
        return {"status": "error", "message": str(exc), "retry": False}


def tool_write_cells(spreadsheet_id: str, range: str, values: str) -> dict:
    """Write values to a Google Sheets range.

    Args:
        spreadsheet_id: The ID of the spreadsheet (from the URL).
        range: A1 notation range to write to (e.g. 'Sheet1!A1:B2').
        values: A JSON string representing a 2D array of cell values,
            e.g. '[["Name","Age"],["Alice","30"]]'.

    Returns:
        A dict with status, updated_range, updated_rows, and updated_cols.
    """
    try:
        parsed_values = json.loads(values)
        if not isinstance(parsed_values, list):
            return {
                "status": "error",
                "message": "values must be a JSON array of arrays",
                "retry": False,
            }

        headers = google_auth_headers(_SERVICE)
        body = {
            "range": range,
            "majorDimension": "ROWS",
            "values": parsed_values,
        }
        resp = httpx.put(
            f"{_BASE}/{spreadsheet_id}/values/{range}",
            headers=headers,
            params={"valueInputOption": "USER_ENTERED"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "status": "ok",
            "updated_range": data.get("updatedRange"),
            "updated_rows": data.get("updatedRows"),
            "updated_cols": data.get("updatedColumns"),
        }
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "message": f"Invalid JSON in values parameter: {exc}",
            "retry": False,
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_write_cells failed")
        return {"status": "error", "message": str(exc), "retry": False}


def tool_get_spreadsheet_info(spreadsheet_id: str) -> dict:
    """Get metadata about a Google Sheets spreadsheet (title and sheet list).

    Args:
        spreadsheet_id: The ID of the spreadsheet (from the URL).

    Returns:
        A dict with status, title, and sheets list (each with sheetId, title,
        rowCount, columnCount).
    """
    try:
        headers = google_auth_headers(_SERVICE)
        resp = httpx.get(
            f"{_BASE}/{spreadsheet_id}",
            headers=headers,
            params={"fields": "spreadsheetId,properties.title,sheets.properties"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        sheets = [
            {
                "sheetId": s["properties"]["sheetId"],
                "title": s["properties"]["title"],
                "rowCount": s["properties"].get("gridProperties", {}).get("rowCount"),
                "columnCount": s["properties"].get("gridProperties", {}).get("columnCount"),
            }
            for s in data.get("sheets", [])
        ]
        return {
            "status": "ok",
            "title": data.get("properties", {}).get("title"),
            "sheets": sheets,
        }
    except IntegrationStatusError as e:
        return e.as_tool_result()
    except Exception as exc:
        logger.exception("tool_get_spreadsheet_info failed")
        return {"status": "error", "message": str(exc), "retry": False}
