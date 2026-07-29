"""
bi/data/google_drive_executor.py
--------------------------------
Google Drive & Google Sheets query executor for BI charts.

Fetches data using pre-configured OAuth tokens stored in tool_config.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from th2agent.bi.charts.core import DataSource
from th2agent.tools_store.tools_helpers import fetch_tool_configs

logger = logging.getLogger(__name__)


class GoogleDriveQueryExecutor:
    """
    Executes queries against Google Drive files or Google Sheets.
    Requires a valid tool connection config with OAuth tokens.
    """

    def __init__(
        self,
        tool_config_id: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        self._tool_config_id = tool_config_id
        self._owner_id = owner_id

    async def run(self, source: DataSource) -> list[dict[str, Any]]:
        if not self._tool_config_id:
            logger.error("Google Drive executor requires a connection_config_id")
            return [{"error": "Missing connection_config_id for Google Drive source"}]
        if not self._owner_id:
            logger.error("Google Drive executor requires an owner_id for tenant isolation")
            return [{"error": "Missing owner_id for Google Drive source"}]

        # Fetch the OAuth token from your workspace tool config system
        config = fetch_tool_configs(self._tool_config_id, owner_id=self._owner_id)
        if not config or "tool_config_params" not in config:
            return [{"error": f"Invalid or missing tool config: {self._tool_config_id}"}]
            
        params = config["tool_config_params"]
        # In th2agent, OAuth tokens are usually stored here
        access_token = params.get("access_token") or params.get("token")
        
        if not access_token:
            return [{"error": "Missing access token in Google Drive connection."}]

        opts = source.source_options or {}
             
        kind = opts.get("source_kind")
        
        if kind == "sheet":
            return await self._read_sheet(opts, access_token, source.limit)
        elif kind == "file":
            return await self._read_file(opts, access_token, source.limit)
        else:
            return [{"error": f"Unknown or missing source_kind '{kind}' for Google Drive. Expected 'sheet' or 'file'"}]

    async def _read_sheet(self, opts: dict, token: str, limit: int | None) -> list[dict[str, Any]]:
        import httpx
        
        sheet_id = opts.get("spreadsheet_id")
        if not sheet_id:
            return [{"error": "Missing spreadsheet_id in source_options"}]

        gid = opts.get("gid", "0") 
        # Using the export endpoint is the easiest way to pull a sheet cleanly as CSV
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                
                if resp.status_code == 401:
                    return [{"error": "Unauthorized. Google Drive token is expired — please refresh your Google Drive connection in settings."}]
                elif resp.status_code == 404:
                    return [{"error": f"Google Sheet '{sheet_id}' not found. Check permissions."}]
                elif resp.status_code != 200:
                    return [{"error": f"Google Sheets API error: {resp.status_code} {resp.text}"}]
                    
                text = resp.text
                return self._parse_csv(text, limit)
                
        except Exception as e:
            logger.error(f"Failed to fetch Google Sheet {sheet_id}: {e}")
            return [{"error": f"Failed to fetch Google Sheet: {str(e)}"}]

    async def _read_file(self, opts: dict, token: str, limit: int | None) -> list[dict[str, Any]]:
        import httpx
        
        file_id = opts.get("file_id")
        if not file_id:
            return [{"error": "Missing file_id in source_options"}]

        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                
                if resp.status_code == 401:
                    return [{"error": "Unauthorized. Google Drive token is expired — please refresh your Google Drive connection in settings."}]
                elif resp.status_code == 404:
                    return [{"error": f"Google Drive file '{file_id}' not found."}]
                elif resp.status_code != 200:
                    return [{"error": f"Google Drive API error: {resp.status_code} {resp.text}"}]
                    
                text = resp.content.decode('utf-8-sig', errors='replace')
                return self._parse_csv(text, limit)
                
        except Exception as e:
            logger.error(f"Failed to fetch Google Drive file {file_id}: {e}")
            return [{"error": f"Failed to fetch file: {str(e)}"}]

    def _parse_csv(self, text: str, limit: int | None) -> list[dict[str, Any]]:
        if not text.strip():
            return []
            
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = ","
            
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        if not reader.fieldnames:
            return [{"error": "No columns found in the file."}]
            
        rows = []
        max_rows = limit or 10000
        
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append({k: self._auto_cast(v) for k, v in row.items()})
            
        return rows

    def _auto_cast(self, value: Any) -> Any:
        """Cast CSV strings to int/float so charts render properly."""
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            pass
        try:
            return float(value)
        except (ValueError, TypeError):
            pass
        return value
    

