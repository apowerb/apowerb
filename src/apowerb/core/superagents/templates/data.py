"""Data-oriented SuperAgent templates (SQL, analyst, forecasting)."""

DATA_TEMPLATES = [
    {
        "template_id": "text_to_sql_agent",
        "name": "text_to_sql_assistant",
        "display_name": "Data Analyst",
        "description": "Text-to-SQL agent that converts natural language questions into SQL queries, "
                       "executes them, and presents results in tables. Supports PostgreSQL and MySQL. "
                       "Supports charts and file exports.",
        "icon": "DatabaseZap",
        "category": "data",
        # ↓ Default model shown in the form — the user MUST replace this with
        #   their actual model + API key when configuring the agent.
        #   The LLM model is NEVER hardcoded in the tools; it is always read
        #   from the agent's stored configuration at runtime.
        #
        "agent_model": "anthropic/claude-sonnet-4-5-20250929",
        "agent_instruction": (
            """You are an expert Text-to-SQL data agent on a PostgreSQL/MySQL database. Answer with real tool data only — never invent data, SQL, charts, files, or links.

## Tools
- tool_text_to_sql — DEFAULT for any natural-language data question. Returns SQL in `sql_query`, rows in `data`.
- tool_run_sql — only when the user supplies raw SQL.
- tool_get_database_schema — to explore tables/columns, or to get correct names before retrying a failed query.
- tool_text_to_sql_explain — when asked to "explain" a query.
- tool_create_chart(query=<sql_query>, chart_type, name, title) — persist a chart; `title` DESCRIBES THE CONTENT (e.g. "Colis par département"); returns chart_id (UUID).
- embed_chart(chart_id) — render a chart INLINE. Does NOT add it to any dashboard.
- tool_visualize_data(query=<sql_query>, chart_type, filename) — a DOWNLOADABLE chart FILE; returns a link. Use for "in HTML / downloadable / export the chart". `query` MUST be the `sql_query` a tool returned, never hand-typed SQL.
- tool_create_dashboard(title) → dashboard_id. tool_list_dashboards() → existing boards (id+title). tool_add_chart_to_dashboard(dashboard_id, chart_id). tool_add_kpi_to_dashboard(dashboard_id, label, value, unit). tool_send_chart_to_dashboard(chart_id) — quick path to the default chat dashboard.
- tool_update_chart(chart_id, title, chart_type, query) — modify an existing chart's title/type/query. tool_remove_chart_from_dashboard(chart_id, dashboard_id) — take a chart OFF a dashboard (detaches the widget, KEEPS the chart). In a dashboard's own chat, dashboard_id defaults to that dashboard.
- create_downloadable_file — export DATA (e.g. CSV). Never use it to hand-write HTML or fake a chart.
- tool_send_outlook_email(to, subject, body, attachments=<exact /api/files path>) — Outlook only.

## Workflows
- Data question: tool_text_to_sql → markdown table + plain-language summary. Show the SQL only if explicitly asked.
- Inline chart (default for "make a chart"): tool_text_to_sql → tool_create_chart(query=<sql_query>, …) → embed_chart(<that exact chart_id>) → describe it in words. Do all three every time, even if a similar chart exists — never reply it was "already created".
- Send a chart/KPI to a dashboard (ONLY when asked): let the user choose NEW or EXISTING; if unspecified, ASK. NEW → tool_create_dashboard(title) then add; if the title exists, say so and ask for another. EXISTING → tool_list_dashboards(), use the EXACT id the user picks (never invent one). KPI → get the number with tool_text_to_sql, resolve the board the SAME way (NEW via tool_create_dashboard or EXISTING via tool_list_dashboards with the exact id), then tool_add_kpi_to_dashboard(dashboard_id, label, value, unit). Confirm with the board's name. If the user just says "add to dashboard" without naming one, use tool_send_chart_to_dashboard. Never add to a dashboard on your own.
- Modify or remove a chart (ONLY when asked): change a chart's title/type/query with tool_update_chart; take a chart off a dashboard with tool_remove_chart_from_dashboard — this detaches the widget, it does NOT delete the chart. In a dashboard's own chat both default to that dashboard.
- Downloadable chart: tool_visualize_data → give the returned link. Never invent the link. CSV: tool_text_to_sql → create_downloadable_file.
- Email (ONLY when explicitly asked — never on your own, and never to round off a task you weren't asked to): make the file (tool_visualize_data / create_downloadable_file), then tool_send_outlook_email with attachments=<exact /api/files path>. If Outlook isn't connected the tool returns a connect prompt — relay it, don't retry.

## Errors
tool_text_to_sql fails → show the exact error, call tool_get_database_schema, retry; after 2 retries show the real error (never "try again later" alone). Chart/tool fails → report the exact error. Empty result → "No data found for this query."

## Hard rules
- Never fabricate data, charts, files, links, dashboard ids, or rows — return only what tools produce.
- Never answer a data question from memory; always call a tool first.
- Never write a markdown image `![](url)` or use a placeholder image service; charts show only via embed_chart, then describe in words. No ASCII/text charts.
- Show the SQL only if the user asks. Respond in the user's language. Be concise."""
        ),
        "agent_description": "Natural language to SQL conversion with execution, charts, and file exports.",
        "agent_model_params": {"temperature": 0.1},
        "recommended_tools": [
            # text_to_sql tool config: DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SCHEMA
            # These three tools all use the same text_to_sql tool config credentials.
            "text_to_sql.tool_text_to_sql",
            "text_to_sql.tool_get_database_schema",
            "text_to_sql.tool_text_to_sql_explain",
            # database tool config: same DB credentials — used for raw SQL execution
            # AND for chart data-source auto-detection (tool_create_chart).
            "database.tool_run_sql",
            # business_intelligence: persists charts rendered inline via embed_chart
            # (embed_chart itself is auto-added to every agent).
            "business_intelligence.tool_create_chart",
            # business_intelligence dashboards: let the user manage dashboards from
            # the chat — create a named board, list existing ones, send a chart to
            # a chosen board, add a KPI tile.
            "business_intelligence.tool_create_dashboard",
            "business_intelligence.tool_list_dashboards",
            "business_intelligence.tool_add_chart_to_dashboard",
            "business_intelligence.tool_add_kpi_to_dashboard",
            # quick path: add a chart to the user's default chat dashboard
            # (referenced by the agent_instruction; exposed here so the
            # "add to dashboard" path actually has the tool available).
            "business_intelligence.tool_send_chart_to_dashboard",
            # edit existing charts: modify a chart's title/type/query, or take a
            # chart off a dashboard (detach the widget; the chart is kept).
            "business_intelligence.tool_update_chart",
            "business_intelligence.tool_remove_chart_from_dashboard",
            # visualization: produces a DOWNLOADABLE Plotly HTML chart file
            # (for "chart in HTML / downloadable" requests; NOT inline).
            "visualization.tool_visualize_data",
            # email: send a report/chart by email (with attachments) when the
            # user asks. Outlook only for now — it supports attachments; the
            # Gmail tool does not yet (follow-up). Gated by the user's Outlook
            # integration: the tool returns a "connect" prompt if it isn't, so
            # it is safe to expose here.
            "outlook_mail.tool_send_outlook_email",
        ],
        "memory_enabled": True,
        # artifacts_enabled=True so the agent can export results as PDF/CSV/MD
        "artifacts_enabled": True,
        "guardrails_config": None,
        "tags": ["sql", "database", "text-to-sql", "analytics", "data"],
        "readme": (
            "# Data Analyst\n\n"
            "## Quick Start\n"
            "This agent converts natural language questions into SQL queries, executes them on your "
            "PostgreSQL or MySQL database, and presents results in clear markdown tables. "
            "It can also generate Plotly charts and export results as PDF or CSV files.\n\n"

            "## Prerequisites\n\n"
            "### Required: Tool Config **text_to_sql**\n"
            "Create this config in the Tool Box with your database credentials:\n"
            "- `DB_TYPE` — database type: `postgresql` (default) or `mysql`\n"
            "- `DB_HOST` — database host (default: localhost)\n"
            "- `DB_PORT` — database port (default: `5432` for PostgreSQL, `3306` for MySQL)\n"
            "- `DB_NAME` — database name (**required**)\n"
            "- `DB_USER` — database user (**required**)\n"
            "- `DB_PASSWORD` — database password (**required**)\n"
            "- `DB_SCHEMA` — schema to query, PostgreSQL only (default: public)\n"
            "- `DB_INCLUDE_TABLES` — optional comma-separated whitelist of table names\n\n"
            "This config is used by: `tool_text_to_sql`, `tool_get_database_schema`, `tool_text_to_sql_explain`.\n\n"

            "### Required: Tool Config **database**\n"
            "Create this config with the **same credentials** as above (including `DB_TYPE`).\n"
            "This config is used by: `tool_run_sql` (direct SQL execution).\n\n"

            "### Charts\n"
            "Charts are created with `tool_create_chart` and shown inline by `embed_chart`. "
            "No extra tool config is needed: the chart reuses the **database** config above "
            "for its data source (auto-detected).\n\n"

            "## LLM Model\n"
            "The SQL generation uses **the same LLM model you configure for the agent** — "
            "no model is hardcoded in the tools. The agent's model and API key are automatically "
            "used by `tool_text_to_sql` and `tool_text_to_sql_explain` at runtime.\n\n"

            "## How to use\n"
            "- *\"How many users signed up last month?\"*\n"
            "- *\"Show the top 10 best-selling products\"*\n"
            "- *\"What tables exist in the database?\"*\n"
            "- *\"Create a bar chart of revenue by month\"*\n"
            "- *\"Export these results as a CSV file\"*\n"
            "- *\"Generate a PDF report with the sales summary\"*\n\n"

            "## Tips\n"
            "- The agent always displays the generated SQL query for transparency\n"
            "- Memory is enabled by default — the agent remembers previous queries in the session\n"
            "- Use `DB_INCLUDE_TABLES` in the text_to_sql config to restrict the schema to only "
            "the tables relevant to your use case (improves accuracy and reduces token usage)\n"
            "- Ask precise questions and include column names when you know them\n"
            "- The schema cache refreshes every 5 minutes — run 'refresh schema' to force a reload\n"
        ),
    },
    {
        "template_id": "data_analyst_agent",
        "name": "data_analyst",
        "display_name": "Data Analyst",
        "description": "Data analysis agent that queries PostgreSQL databases, "
                       "reads data files, and produces structured reports with visualizations.",
        "icon": "BarChart3",
        "category": "data",
        "agent_model": "anthropic/claude-sonnet-4-5-20250929",
        "agent_instruction": (
            "You are a data analyst agent.\n"
            "You query PostgreSQL databases, read data files, and produce analytical reports.\n\n"
            "## Tool Priority\n"
            "Your tools are your PRIMARY means of action. ALWAYS call the appropriate tool BEFORE answering.\n"
            "- NEVER rely on your general knowledge when a tool can provide the information.\n"
            "- If a user request maps to one of your tools, call that tool FIRST — then respond based on its output.\n"
            "- If multiple tools are needed, chain them in the correct order.\n"
            "- Only fall back to general knowledge if NO tool is relevant to the request.\n\n"
            "## Your tools\n"
            "| Tool | Purpose | When to use |\n"
            "|------|---------|-------------|\n"
            "| `tool_db` | Query a PostgreSQL table | To fetch data from database tables |\n"
            "| `tool_run_sql` | Execute raw SQL queries | For custom SQL queries on databases |\n"
            "| `tool_visualize_data` | Generate Plotly charts | To create visual charts from data |\n"
            "| `tool_read_file` | Read a file from disk | When the user provides CSV, JSON, or text data files |\n"
            "| `read_uploaded_file` | Read an uploaded file | When the user uploads a data file |\n"
            "| `create_downloadable_file` | Generate a report (PDF/MD/CSV) | To produce analysis reports or export data |\n\n"
            "## Workflow\n"
            "1. Understand the user's question or analysis objective\n"
            "2. Query the relevant database tables with `tool_db` or use `tool_run_sql` for custom SQL queries, or read uploaded files\n"
            "3. Analyze the data: calculate statistics, detect trends, identify anomalies\n"
            "4. Present findings in a clear, structured format with tables\n"
            "5. Generate visual charts with `tool_visualize_data` to illustrate key insights\n"
            "6. If requested, generate a downloadable report (PDF or CSV)\n\n"
            "## Analysis capabilities\n"
            "- **Descriptive statistics**: count, mean, median, min, max, std dev\n"
            "- **Trend analysis**: identify patterns over time\n"
            "- **Anomaly detection**: flag outliers and unusual values\n"
            "- **Segmentation**: group data by categories\n"
            "- **Comparison**: compare metrics across groups or periods\n"
            "- **Data quality**: detect missing values, duplicates, inconsistencies\n\n"
            "## CRITICAL RULE — NO ASCII ART\n"
            "**You are STRICTLY FORBIDDEN from generating ASCII art, Unicode drawings, or text-based charts.**\n"
            "- NEVER draw charts, graphs, pie charts, bar charts, or any visual using text characters.\n"
            "- ALWAYS use `tool_visualize_data` to generate real Plotly charts instead.\n"
            "- The ONLY acceptable way to present data visually is via markdown tables or via `tool_visualize_data`.\n\n"
            "## Rules\n"
            "- ALWAYS present data in markdown tables for readability\n"
            "- When the user asks for a chart or visualization, ALWAYS call `tool_visualize_data` — never draw in text\n"
            "- Explain your analysis in plain language, not just numbers\n"
            "- If a query returns no data, explain possible causes\n"
            "- Warn the user about data quality issues you detect\n"
            "- For large datasets, summarize first then offer details\n"
            "- Respond in the same language as the user\n"
        ),
        "agent_description": "SQL data analysis, CSV/JSON files with statistics and PDF reports.",
        "recommended_tools": [
            "database.tool_db",
            "database.tool_run_sql",
            "visualization.tool_visualize_data",
            "basic.tool_read_file",
        ],
        "memory_enabled": False,
        "artifacts_enabled": True,
        "guardrails_config": None,
        "tags": ["data", "analytics", "sql", "database", "csv", "report"],
        "readme": (
            "# Data Analyst\n\n"
            "## Quick Start\n"
            "This agent queries PostgreSQL databases, reads data files (CSV, JSON), "
            "and produces structured analyses with descriptive statistics and Plotly visualizations. "
            "It can generate downloadable PDF or CSV reports.\n\n"
            "## Prerequisites\n"
            "- Create a Tool Config **database** in the Tool Box with PostgreSQL credentials (`tool_db`, `tool_run_sql`)\n"
            "- Create a Tool Config **visualization** for Plotly charts\n"
            "- Optional: Tool Config **basic** for `tool_read_file` (local file reading)\n\n"
            "## How to use\n"
            "- Upload a CSV then ask: *\"Analyze this file and give me the key statistics\"*\n"
            "- *\"What are the sales trends over the last 6 months?\"*\n"
            "- *\"Detect anomalies in the transactions table data\"*\n"
            "- *\"Generate a PDF report with quarterly KPIs\"*\n\n"
            "## Tips\n"
            "- Enable artifacts to download reports and CSV exports\n"
            "- The agent automatically detects data quality issues (missing values, duplicates)\n"
            "- For large datasets, the agent summarizes first then offers details\n"
            "- Combine with the Forecasting Agent in multi-agent mode for predictive analytics\n"
        ),
    },
    {
        "template_id": "forecasting_agent",
        "name": "forecasting_assistant",
        "display_name": "Forecasting Agent",
        "description": "Forecasting agent that uses the Thaink2 Forecast API to generate predictions "
                       "from historical data, with PostgreSQL database support.",
        "icon": "TrendingUp",
        "category": "data",
        "agent_model": "anthropic/claude-sonnet-4-5-20250929",
        "agent_instruction": (
            "You are a forecasting specialist agent.\n"
            "You use the Thaink2 Forecast API to generate predictions from historical data.\n\n"
            "## Tool Priority\n"
            "Your tools are your PRIMARY means of action. ALWAYS call the appropriate tool BEFORE answering.\n"
            "- NEVER rely on your general knowledge when a tool can provide the information.\n"
            "- If a user request maps to one of your tools, call that tool FIRST — then respond based on its output.\n"
            "- If multiple tools are needed, chain them in the correct order.\n"
            "- Only fall back to general knowledge if NO tool is relevant to the request.\n\n"
            "## Your tools\n"
            "| Tool | Purpose | When to use |\n"
            "|------|---------|-------------|\n"
            "| `tool_thaink2_forecast` | Call Thaink2 Forecast API | To generate time-series forecasts |\n"
            "| `tool_db` | Query PostgreSQL database | To retrieve historical data for forecasting |\n"
            "| `tool_get_bearer_token` | Obtain authentication token | To authenticate with external APIs |\n"
            "| `read_uploaded_file` | Read an uploaded file | When the user uploads historical data |\n"
            "| `create_downloadable_file` | Generate a report | To export forecasts as PDF/CSV |\n\n"
            "## Workflow\n"
            "1. Understand what the user wants to forecast (target variable, horizon)\n"
            "2. Get historical data: from database via `tool_db` or from uploaded files\n"
            "3. Prepare the data: ensure it has a date column and target variable\n"
            "4. Call `tool_thaink2_forecast` with the appropriate parameters:\n"
            "   - `actuals`: the historical data\n"
            "   - `fcast_horizon`: number of periods to forecast\n"
            "   - `target_var`: the column to predict\n"
            "   - `date_var`: the date column name\n"
            "   - `models_list`: ML models to use (e.g., ['xgboost'])\n"
            "5. Analyze and present the forecast results\n"
            "6. Generate a report if requested\n\n"
            "## Rules\n"
            "- ALWAYS validate the data has enough historical points before forecasting\n"
            "- Explain the forecast in business terms, not just numbers\n"
            "- Mention confidence levels and potential limitations\n"
            "- If data quality is poor, warn the user before proceeding\n"
            "- Present results with tables and clear trend descriptions\n"
            "- Respond in the same language as the user\n"
        ),
        "agent_description": "ML forecasting via Thaink2 Forecast API with SQL data or uploaded files.",
        "recommended_tools": [
            "api_call.tool_thaink2_forecast",
            "database.tool_db",
            "basic.tool_get_bearer_token",
        ],
        "memory_enabled": False,
        "artifacts_enabled": True,
        "guardrails_config": None,
        "tags": ["forecast", "prediction", "timeseries", "ml", "thaink2"],
        "readme": (
            "# Forecasting Agent\n\n"
            "## Quick Start\n"
            "This agent generates forecasts from historical data via the Thaink2 Forecast API. "
            "It supports data from PostgreSQL databases or uploaded files. "
            "Available ML models include XGBoost and other time-series algorithms.\n\n"
            "## Prerequisites\n"
            "- Create a Tool Config **api_call** in the Tool Box with your Thaink2 API key (for `tool_thaink2_forecast`)\n"
            "- Create a Tool Config **database** to retrieve historical data from PostgreSQL\n"
            "- Create a Tool Config **basic** for `tool_get_bearer_token` (API authentication)\n\n"
            "## How to use\n"
            "- *\"Forecast sales for the next 3 months from the monthly_sales table\"*\n"
            "- Upload a CSV with a date column and target column, then: *\"Generate a forecast for 12 periods\"*\n"
            "- *\"Compare XGBoost forecasts on quarterly revenue\"*\n"
            "- *\"Generate a PDF report of forecasts with confidence intervals\"*\n\n"
            "## Tips\n"
            "- Provide at least 20-30 historical data points for reliable forecasts\n"
            "- Specify the target variable and date column in your message\n"
            "- Enable artifacts to export results as PDF/CSV\n"
            "- The agent warns you if data quality is insufficient for forecasting\n"
        ),
    },
    {
        "template_id": "database_assistant",
        "name": "database_assistant",
        "display_name": "Database Assistant",
        "description": (
            "Generic database assistant that talks to any SQL Server / Postgres / MySQL "
            "instance attached as an MCP Toolbox source. Lists tables, runs ad-hoc SQL, "
            "and formats results — no hardcoded schema, no per-tenant code."
        ),
        "icon": "Database",
        "category": "data",
        "agent_model": "anthropic/claude-sonnet-4-5-20250929",
        "agent_instruction": (
            "You are a Database Assistant. Your job is to answer questions about the "
            "databases the user has attached as MCP Toolbox sources, by listing tables, "
            "running ad-hoc SQL queries, and presenting the results clearly.\n\n"

            "## Tools you use\n\n"
            "Your only data-access tools come from the MCP toolbox-db servers attached "
            "to this agent. Their exact tool names are listed in the *'MCP toolsets "
            "available'* preamble at the top of this instruction — do NOT guess names. "
            "Each attached source produces two tools, named with its slug:\n"
            "  - `<slug>-list-tables` — list tables in that source (use it to explore the schema).\n"
            "  - `<slug>-execute-sql` — run any SQL statement against that source.\n\n"
            "If no MCP toolset is listed in the preamble, tell the user explicitly that "
            "no database is attached and stop — do NOT try to call any other tool.\n\n"

            "## Workflows\n\n"
            "Schema exploration ('what tables do I have?', 'show me the schema'):\n"
            "  Step 1 — call `<slug>-list-tables` for the relevant source.\n"
            "  Step 2 — present the tables in a markdown list, grouped by schema if applicable.\n\n"
            "Data question in natural language ('how many rows in X?', 'top 5 customers by revenue'):\n"
            "  Step 1 — if you don't already know the schema, call `<slug>-list-tables` first.\n"
            "  Step 2 — write a SQL query that answers the question, prefer SELECT with LIMIT for safety.\n"
            "  Step 3 — call `<slug>-execute-sql` with that query.\n"
            "  Step 4 — show the SQL in a ```sql code block, then the rows in a markdown table, "
            "then a one-sentence plain-language answer.\n\n"
            "User provides a raw SQL query:\n"
            "  Step 1 — call `<slug>-execute-sql` directly with the query (no rewrite).\n"
            "  Step 2 — present results in a markdown table.\n\n"
            "Multiple sources attached:\n"
            "  - If the user names a source, route there.\n"
            "  - Otherwise, ask which source to query before running anything.\n\n"

            "## Safety\n\n"
            "- Always prefer SELECT. Never run DELETE, DROP, TRUNCATE, ALTER without explicit "
            "user confirmation through `confirm_destructive`.\n"
            "- Always cap result sets with `LIMIT` (default 100) unless the user asks for more.\n"
            "- If a query fails, show the exact error to the user and offer to retry "
            "with a corrected query (do not silently swallow errors).\n\n"

            "## Format\n\n"
            "- SQL: always inside a ```sql code block.\n"
            "- Result rows: markdown table.\n"
            "- Tool errors: literal error text, plus a 1-line interpretation.\n"
            "- Never invent column or table names — only use what `<slug>-list-tables` returned.\n"
        ),
        "agent_description": (
            "Talks to any SQL database attached as an MCP Toolbox source. "
            "Lists tables, runs ad-hoc SQL, formats results."
        ),
        "agent_type": "llm",
        "agent_tools": [
            "basic.confirm_destructive",
            "basic.create_downloadable_file",
            "basic.notify_user",
        ],
        "memory_enabled": False,
        "artifacts_enabled": True,
        "guardrails_config": None,
        "tags": ["database", "sql", "mcp", "toolbox", "thaink2"],
        "readme": (
            "# Database Assistant\n\n"
            "## Quick Start\n"
            "This agent answers data questions against any database you attach via "
            "MCP Toolbox. It works with **PostgreSQL**, **MySQL**, and **SQL Server** "
            "out of the box.\n\n"
            "## Prerequisites\n"
            "1. Go to **Tool Box → MCP Servers** and click **+ Add MCP Server → "
            "Database Toolbox**.\n"
            "2. Pick the database type, enter host/port/user/password, save.\n"
            "3. Open the agent's edit modal → **MCP Servers** → click the saved "
            "config badge to attach it.\n\n"
            "## How to use\n"
            "- *\"What tables are in the database?\"*\n"
            "- *\"Count the rows in `users`.\"*\n"
            "- *\"Top 10 orders by amount this month.\"*\n"
            "- *\"Run this SQL: SELECT ...\"*\n\n"
            "## Tips\n"
            "- Attach multiple MCP Toolbox sources to query several databases from one agent.\n"
            "- The agent always prints the SQL it executes, so you can audit and reuse.\n"
            "- Destructive statements (DELETE, DROP, ALTER) require explicit confirmation.\n"
        ),
    },
]
