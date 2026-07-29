---
name: dashboard-builder
description: "Create interactive BI dashboards with charts, KPIs, and tables. Use when the user asks to build a dashboard, create a reporting page, set up KPI monitoring, or combine multiple charts. Keywords - dashboard, reporting, bi, business intelligence, kpi, monitoring, overview, summary, metrics, create dashboard, build dashboard."
---

# Dashboard Builder

You are an expert at creating comprehensive BI dashboards. Follow these steps to design and build the perfect dashboard.

## Step 1: Understand the Requirements
- What data sources are available? (SQL database, CSV files, API results)
- What is the purpose? (monitoring, reporting, executive summary, operations)
- Who is the audience? (technical team, management, external stakeholders)
- What time range? (real-time, daily, weekly, monthly)

## Step 2: Plan the Dashboard Layout
Dashboard uses a 12-column grid system. Plan components:

### Layout Templates

**Executive Summary (2 rows)**
Row 0: 4 KPI tiles (width=3 each, height=2)
Row 2: 1 main chart (width=8, height=4) + 1 secondary chart (width=4, height=4)

**Operations Monitor (3 rows)**
Row 0: 3 KPI tiles (width=4 each, height=2)
Row 2: 2 charts side by side (width=6 each, height=4)
Row 6: 1 data table full width (width=12, height=4)

**Analytics Deep-Dive (3 rows)**
Row 0: 1 main chart (width=12, height=4)
Row 4: 3 charts (width=4 each, height=4)
Row 8: 1 data table full width (width=12, height=4)

## Step 3: Gather the Data
- Use `tool_run_sql` or `tool_text_to_sql` to query data
- For each metric/chart, run the appropriate query first
- Note the query string for each chart's data source

## Step 4: Create the Charts
Use `tool_create_chart` for each visualization:

### Chart Type Decision Tree
| Data Pattern | Chart Type | When to Use |
|---|---|---|
| Category vs Value | `bar` | Comparing values across categories |
| Values over Time | `line` | Showing trends, time series |
| Proportions | `pie` / `donut` | Parts of a whole (max 7 slices) |
| Single Metric | `stat` | KPI card, single number with context |
| Two Numeric | `scatter` | Correlation analysis |
| Volume over Time | `area` | Cumulative or stacked time series |
| Data Grid | `table` | Detailed records, sortable lists |

### Naming Conventions
- Chart name: snake_case, descriptive (e.g., `monthly_revenue_by_region`)
- Chart title: Human-readable (e.g., "Monthly Revenue by Region")

## Step 5: Build the Dashboard
1. Call `tool_create_dashboard` with a descriptive title and slug
2. For each chart: call `tool_add_chart_to_dashboard` with correct grid position
3. For KPI metrics: call `tool_add_kpi_to_dashboard` with value, unit, and trend
4. Always place KPIs at the top (row 0), charts below, tables at the bottom

## Step 6: Publish
Call `tool_publish_dashboard` with appropriate visibility:
- `private`: Only the creator can see it
- `organization`: All org members can access it
- `public`: Anyone with the link can view it

## Important Rules
- **Always create charts BEFORE adding them to a dashboard** — you need the chart_id
- **Plan the grid layout before creating** — avoid overlapping components
- **KPIs first, charts second, tables last** — this is the standard BI layout convention
- **Use descriptive titles** — "Q1 2026 Sales by Region" not "Chart 1"
- **Set refresh_interval** on charts that show live data (30s for KPIs, 60s for charts)
- **Maximum 8-10 components per dashboard** — too many makes it unusable
- When the user says "dashboard", always create a proper dashboard with multiple components — never just a single chart
