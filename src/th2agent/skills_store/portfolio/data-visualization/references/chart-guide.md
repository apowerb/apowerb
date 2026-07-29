# Chart Type Guide

## Bar Chart

### When to Use
- Comparing discrete categories (products, regions, departments).
- Showing rankings or top-N lists.
- Displaying counts or totals across groups.

### Examples
- Sales by product category
- Employee count by department
- Top 10 customers by revenue

### Configuration
- **Vertical bars**: Default when category labels are short (< 10 characters).
- **Horizontal bars**: Use when labels are long or there are many categories.
- **Sort by value**: Always sort descending unless there is a natural order (e.g., age groups, months).
- **Color**: Use a single color for single-series. Use distinct colors for grouped bars.

### Multiple Series
- **Grouped bars**: Place bars side by side for direct comparison of values.
- **Stacked bars**: Stack bars to show composition of a total.
- Limit to 5 series maximum for readability.

---

## Line Chart

### When to Use
- Tracking changes over time (time series data).
- Comparing trends across multiple groups.
- Showing continuous data with a natural ordering on x-axis.

### Examples
- Daily active users over 90 days
- Monthly revenue by product line
- Temperature over 24 hours

### Configuration
- **X-axis**: Always a date or sequential value, sorted chronologically.
- **Multiple lines**: Up to 5 series. Use distinct colors and include a legend.
- **Gaps in data**: If data has missing dates, call them out — do not interpolate silently.
- **Annotations**: For single important events (launches, incidents), mention them in the description.

### Avoid When
- Fewer than 5 data points (use a bar chart instead).
- X-axis is categorical with no natural order.

---

## Pie Chart

### When to Use
- Showing parts of a whole that sum to 100%.
- When there are 2-7 categories.
- The audience cares about proportions, not exact values.

### Examples
- Market share distribution
- Budget allocation by department
- Traffic source breakdown

### Configuration
- **Maximum 7 slices**: Combine small values into "Other".
- **Label format**: Show both category name and percentage.
- **Ordering**: Start from 12 o'clock position, arrange largest to smallest clockwise.

### Avoid When
- More than 7 categories.
- Values do not sum to a meaningful total.
- Comparing across different groups or time periods (use bar charts instead).
- Differences between slices are very small (hard to distinguish visually).

---

## Scatter Plot

### When to Use
- Exploring relationships between two numeric variables.
- Identifying clusters, outliers, or correlations.
- Showing distribution of data points in 2D space.

### Examples
- Price vs. sales volume
- Height vs. weight
- Marketing spend vs. conversion rate

### Configuration
- **Both axes numeric**: Never use categorical data on a scatter plot.
- **Color grouping**: Add a categorical column as color to reveal group patterns.
- **Size encoding**: Optionally use a third numeric column to control dot size (bubble chart).
- **Axis labels**: Always label with units (e.g., "Revenue ($)", "Duration (minutes)").

### Avoid When
- One axis is categorical (use a bar chart).
- Too few data points (< 10).
- Too many data points with heavy overlap (consider aggregating or sampling).

---

## Histogram

### When to Use
- Understanding the distribution of a single numeric variable.
- Identifying skewness, modes, and spread.

### Examples
- Distribution of order values
- Age distribution of users
- Response time distribution

### Configuration
- **Bin count**: 10-20 bins is usually appropriate. Adjust based on data range and count.
- **X-axis**: The numeric variable, divided into bins.
- **Y-axis**: Count or frequency of values in each bin.

### Avoid When
- The variable is categorical (use a bar chart).
- Fewer than 20 data points.

---

## Color Best Practices

- Use a consistent color palette throughout related charts.
- Use high-contrast colors for accessibility.
- Avoid red/green combinations alone (color blindness).
- Use color meaningfully — same color should mean the same category across charts.
- For sequential data (low to high), use a single-hue gradient.
- For diverging data (negative to positive), use a two-hue gradient with neutral midpoint.
- Reserve red for negative/danger and green for positive/success only when the context is clear.

## When to Use Multiple Charts

If the user asks for a "dashboard" or comprehensive view:

1. Create separate charts for different questions.
2. Keep each chart focused on one insight.
3. Use consistent axis scales when charts are meant to be compared.
4. Present charts in logical order: overview first, then details.
