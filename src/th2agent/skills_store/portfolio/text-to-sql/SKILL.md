---
name: text-to-sql
description: "Generate SQL queries from natural language questions. Use when the user asks a data question, needs a SQL query, wants to explore database tables, columns, aggregations, joins, or any structured data retrieval. Keywords - SQL, database, query, data question, table, column, aggregate, join, filter, GROUP BY, WHERE, SELECT."
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

## Step 2: Analyze the User's Question

Break the question into components:

- **Entities**: Which tables are involved?
- **Relationships**: How do tables connect (foreign keys)?
- **Filters**: What WHERE conditions are needed?
- **Aggregations**: Does the question ask for counts, sums, averages, min/max?
- **Ordering**: Is there an implied sort (e.g., "top 10", "most recent")?
- **Time range**: Does the question reference a date range?

## Step 3: Plan Complex Queries

For complex questions, break into sub-queries before writing SQL:

1. Identify if CTEs (Common Table Expressions) would simplify the logic.
2. Determine if window functions are needed (ranking, running totals, comparisons).
3. Check if the question requires multiple aggregation levels.

## Step 4: Write the SQL Query

### Mandatory Best Practices

- **Always qualify column names** with table aliases to avoid ambiguity.
  ```sql
  SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id
  ```
- **Use LEFT JOIN by default** instead of INNER JOIN — it is safer when data may be missing and avoids silently dropping rows.
- **Handle NULLs explicitly** with `COALESCE` for numeric and string outputs.
  ```sql
  SELECT COALESCE(SUM(t.amount), 0) AS total_amount
  ```
- **Always add ORDER BY** for deterministic, reproducible results.
- **Always add LIMIT** to prevent returning excessively large result sets. Default to `LIMIT 100` unless the user specifies otherwise.
- **Use meaningful aliases** for computed columns (`AS total_revenue`, not `AS col1`).

### PostgreSQL-Specific Patterns

- Use `schema.table` notation when schemas are present.
- Date truncation: `DATE_TRUNC('month', created_at)`
- Date extraction: `EXTRACT(YEAR FROM created_at)`
- Date formatting: `TO_CHAR(created_at, 'YYYY-MM-DD')`
- Case-insensitive matching: `WHERE name ILIKE '%search%'`
- Array operations: `ANY()`, `array_agg()`
- JSON operations: `->`, `->>`, `jsonb_extract_path_text()`

### MySQL-Specific Patterns

- No schema prefix — use database.table if needed.
- Use backtick quoting for reserved words: `` `order` ``, `` `group` ``.
- Date formatting: `DATE_FORMAT(created_at, '%Y-%m-%d')`
- Case-insensitive by default (collation-dependent).
- Use `IFNULL()` as alternative to `COALESCE` for two arguments.
- `LIMIT` with `OFFSET`: `LIMIT 10 OFFSET 20`

## Step 5: Execute and Handle Errors

- Run the SQL query using `tool_run_sql`, `tool_text_to_sql`, or whichever SQL execution tool is available.
- **If the query fails**, read the error message carefully:
  - **Column not found**: Re-check column names against the schema. Look for typos, wrong table alias, or columns that exist in a different table.
  - **Syntax error**: Check for missing commas, unmatched parentheses, or dialect-specific syntax.
  - **Type mismatch**: Ensure you are comparing compatible types (e.g., don't compare a string to an integer without casting).
- Fix the query and retry **once**. If it fails again, explain the issue to the user and ask for clarification.

## Step 6: Present Results

- Display results in a clear, readable table format.
- Highlight key numbers or findings in your explanation.
- If results return more than 3 rows of numeric data, **offer to create a visualization** using the data-visualization skill.
- If the user might want to explore further, suggest follow-up queries.

## Guidelines

- Never fabricate table or column names — only use what the schema provides.
- When the user's question is ambiguous, ask for clarification rather than guessing.
- For date-related queries, always clarify the timezone assumption if it matters.
- If a question requires data that does not exist in the schema, inform the user what is missing.
