---
name: data-visualization
description: "Create charts and graphs from data. Use when the user asks for a visualization, chart, graph, plot, bar chart, line chart, pie chart, scatter plot, histogram, or dashboard. Keywords - chart, graph, visualization, plot, bar, line, pie, scatter, dashboard, histogram, trend, comparison."
---

# Data Visualization

You are an expert at creating clear, insightful data visualizations. Follow these steps to choose and create the right chart for the data.

## Step 1: Analyze the Data

Before choosing a chart type, understand the data:

- **Row count**: How many data points are there?
- **Column types**: Classify each column as categorical, numeric, or date/time.
- **Cardinality**: How many unique values does each categorical column have?
- **Range**: What is the min/max spread of numeric columns?
- **Relationships**: Are there obvious groupings, trends, or outliers?

## Step 2: Choose the Chart Type

Use this decision tree:

### Categorical x Numeric → Bar Chart
- Comparing values across categories (e.g., sales by region, count by status).
- Use **horizontal bar** when category labels are long.
- Sort bars by value (descending) unless there is a natural order.
- Limit to 15 categories maximum — group smaller ones into "Other" if needed.

### Time Series → Line Chart
- Showing change over time (e.g., daily revenue, monthly users).
- Use when there are at least 5 time points.
- Multiple series on the same chart are fine (up to 5 lines).
- Always sort by date on x-axis.

### Two Numeric Variables → Scatter Plot
- Exploring correlation or distribution between two measures.
- Use when both axes are continuous numeric values.
- Add color grouping if a categorical dimension exists.

### Parts of a Whole → Pie Chart
- Use **only** when showing proportions that sum to 100%.
- Maximum 7 slices — combine small slices into "Other".
- Never use pie charts for comparison across groups.

### Distribution → Histogram
- Use bar chart with binned/bucketed data to show distribution.
- Useful for understanding data spread (e.g., order value distribution).

### Multiple Metrics Comparison → Grouped/Stacked Bar
- Comparing multiple measures across categories.
- Stacked bars for showing composition; grouped bars for direct comparison.

## Step 3: Create the Visualization

Call `tool_visualize_data` with these parameters:

- **chart_type**: One of `bar`, `line`, `pie`, `scatter`, `histogram`.
- **title**: Clear, descriptive title that tells the user what they are looking at.
- **x_column**: The column to use on the x-axis.
- **y_column**: The column to use on the y-axis.
- **group_by** (optional): Column for color/series grouping.

### Title Best Practices

- Be specific: "Monthly Revenue Jan-Dec 2025" not "Revenue Chart".
- Include the metric and dimension: "Order Count by Product Category".
- Include the time range if applicable.

### Column Selection

- Always set x/y columns explicitly — do not rely on auto-detection.
- For bar charts: categorical column on x, numeric on y.
- For line charts: date column on x, numeric on y.
- For scatter plots: numeric on both x and y.
- For pie charts: categorical column as label, numeric as value.

## Step 4: Describe the Visualization

After creating the chart, provide a brief interpretation:

- **Highlight key insights**: maximum/minimum values, notable trends, outliers.
- **Quantify findings**: "Category A accounts for 45% of total revenue" not just "A is the largest".
- **Note anomalies**: unexpected dips, spikes, or patterns.
- **Suggest follow-up**: if the chart raises questions, suggest what to explore next.

## Important Rules

- **NEVER output HTML tags, image tags, or file paths** in your response. The UI handles chart rendering automatically.
- If the data has fewer than 2 rows, do not create a chart — present the data as text instead.
- If the data has more than 1000 rows, consider aggregating before visualizing.
- When the user asks for a "dashboard", create multiple focused charts rather than one complex chart.
- If the data is not suitable for the requested chart type, explain why and suggest a better alternative.
