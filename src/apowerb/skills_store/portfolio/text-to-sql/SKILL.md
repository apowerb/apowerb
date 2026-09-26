---
name: text-to-sql
description: "Generate SQL queries from natural language questions. Use when the user asks a data question, needs a SQL query, wants to explore database tables, columns, aggregations, joins, or any structured data retrieval."
---

# Text-to-SQL Generation

You are an expert SQL generator. Follow these steps to convert natural language questions into correct, efficient SQL queries.

## Step 1: Understand the Schema

**Always inspect the database schema first** before writing any SQL. Do not assume table or column names.

- If `tool_get_database_schema` is available, call it to get the full schema.
- If only `tool_run_sql` is available, run these discovery queries:
  - **PostgreSQL**: `SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name`
  - **MySQL**: `SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY table_name`
  - Then for each relevant table: `SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = '<table>' ORDER BY ordinal_position`

- Review all available tables, their columns, and data types.
- Identify primary keys and foreign key relationships.
- Note any naming conventions (snake_case, camelCase, prefixes).

## Step 2: Write the SQL Query

### Mandatory Best Practices

- **Always qualify column names** with table aliases to avoid ambiguity.
  ```sql
  SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id
  ```
- **Handle NULLs explicitly** with `COALESCE` for numeric and string outputs.
  ```sql
  SELECT COALESCE(SUM(t.amount), 0) AS total_amount
  ```
- **Always add ORDER BY** for deterministic, reproducible results.
- **Always add LIMIT** to prevent returning excessively large result sets. Default to `LIMIT 100` unless the user specifies otherwise.
- **Use meaningful aliases** for computed columns (`AS total_revenue`, not `AS col1`).
- **Qualify PostgreSQL tables as `schema.table`** when schemas are present.

## Step 3: Execute and Handle Errors

- Run the SQL query using `tool_run_sql`, `tool_text_to_sql`, or whichever SQL execution tool is available.
- **If the query fails**, read the error message carefully:
  - **Column not found**: Re-check column names against the schema. Look for typos, wrong table alias, or columns that exist in a different table.
  - **Syntax error**: Check for missing commas, unmatched parentheses, or dialect-specific syntax.
  - **Type mismatch**: Ensure you are comparing compatible types (e.g., don't compare a string to an integer without casting).
- Fix the query and retry **once**. If it fails again, explain the issue to the user and ask for clarification.

## Step 4: Present Results

- Display results in a clear, readable table format.
- Highlight key numbers or findings in your explanation.
- If results return more than 3 rows of numeric data, **offer to create a visualization** using the data-visualization skill.
- If the user might want to explore further, suggest follow-up queries.

## Guidelines

- Never fabricate table or column names — only use what the schema provides.
- When the user's question is ambiguous, ask for clarification rather than guessing.
- For date-related queries, always clarify the timezone assumption if it matters.
- If a question requires data that does not exist in the schema, inform the user what is missing.
