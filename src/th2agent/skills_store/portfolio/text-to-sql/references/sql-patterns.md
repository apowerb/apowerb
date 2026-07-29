# Common SQL Patterns

## GROUP BY with HAVING

Use HAVING to filter groups after aggregation.

```sql
-- Find customers with more than 5 orders
SELECT c.id, c.name, COUNT(o.id) AS order_count
FROM customers c
LEFT JOIN orders o ON c.id = o.customer_id
GROUP BY c.id, c.name
HAVING COUNT(o.id) > 5
ORDER BY order_count DESC;
```

## Window Functions

### ROW_NUMBER — Ranking and Deduplication

```sql
-- Get the most recent order per customer
WITH ranked AS (
  SELECT
    o.*,
    ROW_NUMBER() OVER (PARTITION BY o.customer_id ORDER BY o.created_at DESC) AS rn
  FROM orders o
)
SELECT * FROM ranked WHERE rn = 1;
```

### LAG / LEAD — Comparing Adjacent Rows

```sql
-- Month-over-month revenue change
SELECT
  DATE_TRUNC('month', created_at) AS month,
  SUM(amount) AS revenue,
  LAG(SUM(amount)) OVER (ORDER BY DATE_TRUNC('month', created_at)) AS prev_month_revenue,
  SUM(amount) - LAG(SUM(amount)) OVER (ORDER BY DATE_TRUNC('month', created_at)) AS change
FROM orders
GROUP BY DATE_TRUNC('month', created_at)
ORDER BY month;
```

### Running Totals

```sql
-- Cumulative revenue over time
SELECT
  date,
  daily_revenue,
  SUM(daily_revenue) OVER (ORDER BY date ROWS UNBOUNDED PRECEDING) AS cumulative_revenue
FROM daily_sales
ORDER BY date;
```

### RANK and DENSE_RANK

```sql
-- Rank products by sales, handling ties
SELECT
  p.name,
  SUM(oi.quantity) AS total_sold,
  RANK() OVER (ORDER BY SUM(oi.quantity) DESC) AS rank,
  DENSE_RANK() OVER (ORDER BY SUM(oi.quantity) DESC) AS dense_rank
FROM products p
LEFT JOIN order_items oi ON p.id = oi.product_id
GROUP BY p.name
ORDER BY rank;
```

## Common Table Expressions (CTEs)

### Basic CTE

```sql
-- Multi-step calculation: find high-value customers, then get their recent orders
WITH high_value_customers AS (
  SELECT customer_id
  FROM orders
  GROUP BY customer_id
  HAVING SUM(amount) > 10000
)
SELECT o.*
FROM orders o
JOIN high_value_customers hvc ON o.customer_id = hvc.customer_id
WHERE o.created_at >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY o.created_at DESC;
```

### Recursive CTE

```sql
-- Traverse a category hierarchy
WITH RECURSIVE category_tree AS (
  SELECT id, name, parent_id, 1 AS depth
  FROM categories
  WHERE parent_id IS NULL

  UNION ALL

  SELECT c.id, c.name, c.parent_id, ct.depth + 1
  FROM categories c
  JOIN category_tree ct ON c.parent_id = ct.id
)
SELECT * FROM category_tree ORDER BY depth, name;
```

## Pivot Queries

### PostgreSQL — Using FILTER

```sql
-- Pivot monthly sales by product category
SELECT
  DATE_TRUNC('month', o.created_at) AS month,
  SUM(oi.amount) FILTER (WHERE p.category = 'Electronics') AS electronics,
  SUM(oi.amount) FILTER (WHERE p.category = 'Clothing') AS clothing,
  SUM(oi.amount) FILTER (WHERE p.category = 'Food') AS food
FROM orders o
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id
GROUP BY month
ORDER BY month;
```

### MySQL — Using CASE

```sql
-- Pivot monthly sales by product category
SELECT
  DATE_FORMAT(o.created_at, '%Y-%m') AS month,
  SUM(CASE WHEN p.category = 'Electronics' THEN oi.amount ELSE 0 END) AS electronics,
  SUM(CASE WHEN p.category = 'Clothing' THEN oi.amount ELSE 0 END) AS clothing,
  SUM(CASE WHEN p.category = 'Food' THEN oi.amount ELSE 0 END) AS food
FROM orders o
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id
GROUP BY month
ORDER BY month;
```

## Date Series Generation

### PostgreSQL

```sql
-- Generate a series of dates and LEFT JOIN to fill gaps
SELECT
  d.date,
  COALESCE(COUNT(o.id), 0) AS order_count
FROM generate_series(
  CURRENT_DATE - INTERVAL '30 days',
  CURRENT_DATE,
  '1 day'::interval
) AS d(date)
LEFT JOIN orders o ON DATE_TRUNC('day', o.created_at) = d.date
GROUP BY d.date
ORDER BY d.date;
```

### MySQL

```sql
-- Using a recursive CTE to generate date series
WITH RECURSIVE date_series AS (
  SELECT CURDATE() - INTERVAL 30 DAY AS date
  UNION ALL
  SELECT date + INTERVAL 1 DAY FROM date_series WHERE date < CURDATE()
)
SELECT
  ds.date,
  COALESCE(COUNT(o.id), 0) AS order_count
FROM date_series ds
LEFT JOIN orders o ON DATE(o.created_at) = ds.date
GROUP BY ds.date
ORDER BY ds.date;
```

## Percentile and Distribution

```sql
-- PostgreSQL: Calculate percentiles
SELECT
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount) AS median,
  PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY amount) AS p90,
  PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY amount) AS p99
FROM orders;
```

## Conditional Aggregation

```sql
-- Count and percentage in one query
SELECT
  COUNT(*) AS total_orders,
  COUNT(*) FILTER (WHERE status = 'completed') AS completed,
  COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
  ROUND(100.0 * COUNT(*) FILTER (WHERE status = 'completed') / COUNT(*), 1) AS completion_rate
FROM orders;
```
