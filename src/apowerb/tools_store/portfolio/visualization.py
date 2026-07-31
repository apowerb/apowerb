import os
import re
from logging import getLogger
from pathlib import Path
from typing import Any

from apowerb.configs.paths import uploads_dir

logger = getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", os.path.basename(name))
    return safe or "chart"


def _create_plotly_html(df, chart_type: str, x_col: str | None, y_col: str | None, title: str, output_path: Path):
    """Create interactive HTML chart using plotly."""
    import plotly.express as px
    import plotly.graph_objects as go

    num_cols = df.select_dtypes(include="number").columns.tolist()
    cat_cols = df.select_dtypes(include=["object", "string"]).columns.tolist()
    
    x = x_col or (cat_cols[0] if cat_cols else (df.columns[0] if len(df.columns) > 0 else None))
    y = y_col or (num_cols[0] if num_cols else (df.columns[1] if len(df.columns) > 1 else None))

    if x is None or y is None:
        raise ValueError(f"Cannot determine chart axes: x={x}, y={y}. DataFrame columns: {list(df.columns)}")

    ct = chart_type.lower()

    if ct == "bar":
        fig = px.bar(df, x=x, y=y, title=title)
    elif ct == "line":
        fig = px.line(df, x=x, y=y, title=title)
    elif ct == "scatter":
        fig = px.scatter(df, x=x, y=y, title=title)
    elif ct == "pie":
        fig = px.pie(df, values=y or num_cols[0], names=x or cat_cols[0], title=title)
    else:  # Default bar
        fig = px.bar(df, x=x, y=y, title=title)
    
    fig.write_html(
        str(output_path),
        include_plotlyjs="cdn",
        config={
            "displayModeBar": True,
            "responsive": True,
            "toImageButtonOptions": {
                "format": "png",
                "filename": title.replace(" ", "_"),
                "height": 800,
                "width": 1200,
                "scale": 2,
            },
        },
    )


def tool_visualize_data(
    data: list[dict[str, Any]] | None = None,
    filename: str = "chart",
    title: str = "",
    chart_type: str = "auto",
    x_column: str = "",
    y_column: str = "",
    folder_name: str = "",
    query: str = "",
) -> dict:
    """
    Creates a DOWNLOADABLE interactive HTML chart file from SQL query results.

    Use for "chart in HTML / downloadable / export the chart" requests. For
    charts shown INLINE in the chat, use tool_create_chart + embed_chart instead
    (this is a download, it does not render inline). Pass the 'data' list from a
    tool_text_to_sql / tool_run_sql result directly into this tool.

    Supported chart_type values: "auto", "bar", "line", "scatter", "pie".
    Use "auto" to let the tool decide.

    Args:
        data:        List of row dicts – the 'data' field from a tool_run_sql result.
        filename:    Base filename (e.g. "sales_chart") - extensions added automatically.
        title:       Chart title shown above the visualization.
        chart_type:  One of auto/bar/line/scatter/pie.
        x_column:    Column name to use for the X-axis (optional – auto-detected if blank).
        y_column:    Column name to use for the Y-axis (optional – auto-detected if blank).
        folder_name: Agent folder name (passed automatically by tool config).

    Returns:
        dict with download_path (HTML for download).
    """

    try:
        import pandas as pd
    except ImportError:
        return {"success": False, "error": "pip install pandas"}
    
    try:
        import plotly.express as px
    except ImportError:
        return {"success": False, "error": "pip install plotly"}

    # Prefer REAL data from a server-side SQL query over LLM-passed `data`:
    # small models (Mistral) fabricate the `data` arg, producing a download
    # whose content differs from the inline chart. query wins when provided.
    if query and query.strip():
        try:
            from apowerb.tools_store.portfolio.text_to_sql import _agent_execute_sql
            from apowerb.sqlgen.safety import validate_sql_safety
            ok, err = validate_sql_safety(query)
            if not ok:
                return {"success": False, "error": f"Unsafe SQL: {err}"}
            data = _agent_execute_sql(folder_name or "", query, limit=1000)
        except Exception as e:
            return {"success": False, "error": f"Could not run the chart query: {e}"}

    if not data:
        return {"success": False, "error": "No data provided (pass query=<sql> or data)."}

    try:
        df = pd.DataFrame(data)
    except Exception as e:
        return {"success": False, "error": f"Could not build DataFrame: {e}"}

    if df.empty:
        return {"success": False, "error": "Data is empty"}

    # Auto-detect chart type
    if chart_type.lower() == "auto":
        num_cols = df.select_dtypes(include="number").columns.tolist()
        cat_cols = df.select_dtypes(include=["object", "string"]).columns.tolist()
        chart_type = "bar" if (cat_cols and num_cols) else "line"

    chart_title = title or filename.replace(".html", "").replace("_", " ").title()
    
    # Sanitize base filename
    base_name = _sanitize_filename(filename)
    base_name = base_name.replace(".html", "")
    
    html_name = f"{base_name}.html"

    # Always save to agent's folder
    agent_folder = folder_name or "outputs"
    out_dir = uploads_dir() / agent_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    
    html_path = out_dir / html_name

    # Generate HTML (plotly)
    try:
        _create_plotly_html(df, chart_type=chart_type,
                            x_col=x_column or None, y_col=y_column or None, 
                            title=chart_title, output_path=html_path)
    except Exception as e:
        return {"success": False, "error": f"HTML generation failed: {e}"}

    html_size_kb = html_path.stat().st_size / 1024
    download_path = f"/api/files/{agent_folder}/{html_name}"

    logger.info(f"[VISUALIZE] HTML saved: {html_path} ({html_size_kb:.1f} KB)")

    # In S3 storage mode the GET /api/files endpoint serves from S3, so the
    # local file alone would 404 on download. Mirror create_downloadable_file:
    # push the HTML to S3 too. Best-effort — the local copy still exists.
    try:
        from apowerb.configs.settings import get_settings
        if get_settings().storage_mode != "local":
            from apowerb.storage.s3 import upload_bytes_to_s3
            upload_bytes_to_s3(
                html_path.read_bytes(),
                f"uploads/{agent_folder}/{html_name}",
                content_type="text/html",
            )
            logger.info(f"[VISUALIZE] HTML uploaded to S3: uploads/{agent_folder}/{html_name}")
    except Exception as e:
        logger.error(f"[VISUALIZE] S3 upload failed for {html_name}: {e}")

    return {
        "success": True,
        "filename_html": html_name,
        "folder": agent_folder,
        "download_path": download_path,
        "chart_type": chart_type,
        "chart_title": chart_title,
        "row_count": len(df),
        "size_kb": round(html_size_kb, 1),
        "message": "Chart created and displayed successfully. The user can already see it and download it from the UI — just describe what the chart shows. NEVER output HTML, image tags, markdown links, or file paths in your response.",
    }


def tool_export_chart_from_csv(
    csv_path: str,
    filename: str,
    title: str = "",
    chart_type: str = "auto",
    x_column: str = "",
    y_column: str = "",
    folder_name: str = "",
) -> dict:
    """
    Loads a CSV file and exports it as an HTML chart.

    Args:
        csv_path:    Path to the CSV file on disk.
        filename:    Base filename for output.
        title:       Chart title (optional).
        chart_type:  One of auto/bar/line/scatter/pie.
        x_column:    Column for X-axis (auto-detected if blank).
        y_column:    Column for Y-axis (auto-detected if blank).
        folder_name: Agent folder name.

    Returns:
        dict with download_path.
    """
    try:
        import pandas as pd
    except ImportError:
        return {"success": False, "error": "pip install pandas"}

    if not os.path.exists(csv_path):
        return {"success": False, "error": f"CSV file not found: {csv_path}"}

    try:
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip()
    except Exception as e:
        return {"success": False, "error": f"Failed to read CSV: {e}"}

    return tool_visualize_data(
        data=df.to_dict(orient="records"),
        filename=filename,
        title=title or Path(csv_path).stem.replace("_", " ").title(),
        chart_type=chart_type,
        x_column=x_column,
        y_column=y_column,
        folder_name=folder_name,
    )


def make_visualization_tools(folder_name: str) -> list:
    """
    Returns visualization tools bound to the agent's folder.

    Usage:
        from apowerb.tools_store.portfolio.visualization import make_visualization_tools
        tools_funcs.extend(make_visualization_tools("agent280"))
    """
    if not folder_name:
        raise ValueError("folder_name is required")

    import apowerb.tools_store.portfolio.visualization as _viz_module

    def tool_visualize_data(
        filename: str,
        query: str = "",
        title: str = "",
        chart_type: str = "auto",
        data: list[dict[str, Any]] | None = None,
        x_column: str = "",
        y_column: str = "",
    ) -> dict:
        """Creates a DOWNLOADABLE interactive HTML chart FILE.

        Pass query=<the exact sql_query returned by tool_text_to_sql>: the chart
        is built from the REAL query results (executed server-side), so the
        downloaded file matches the data shown inline. Do NOT hand-fill data=
        with example rows (the download would show fabricated data).
        """
        return _viz_module.tool_visualize_data(
            data=data, filename=filename, title=title,
            chart_type=chart_type, x_column=x_column, y_column=y_column,
            folder_name=folder_name, query=query,
        )

    def tool_export_chart_from_csv(
        csv_path: str,
        filename: str,
        title: str = "",
        chart_type: str = "auto",
        x_column: str = "",
        y_column: str = "",
    ) -> dict:
        """Loads a CSV file and exports it as an HTML chart."""
        return _viz_module.tool_export_chart_from_csv(
            csv_path=csv_path, filename=filename, title=title,
            chart_type=chart_type, x_column=x_column, y_column=y_column,
            folder_name=folder_name,
        )

    return [tool_visualize_data, tool_export_chart_from_csv]