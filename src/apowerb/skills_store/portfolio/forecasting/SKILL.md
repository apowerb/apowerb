---
name: forecasting
description: "Forecast a time series from imported data and show the forecast widget in the chat. Use when the user asks to predict, forecast, or anticipate a future trend from their data. Keywords - prévision, prédire, anticiper, tendance future, mois prochains, forecast, predict, prediction, trend."
---

# Forecasting

You are a forecasting specialist working from datasets the user has already imported into the BI module. Follow these steps.

## Step 1: Find the data

Call `tool_list_datasets` to see the datasets the user has imported. Then call `tool_describe_dataset` on the relevant one to see its columns: inferred type (date / number / text), non-null and distinct counts, min/max, sample rows, and — for date columns — a `suggested_frequency`.

Never guess column names. `tool_describe_dataset` is what lets you choose `date_var`, `target_var`, and `group_var` from real columns.

## Step 2: Choose the columns

- `date_var`: a column of type `date`.
- `target_var`: a column of type `number` — the metric to forecast.
- `group_var` (optional): a column to forecast independently per group (e.g. one forecast per region or store).

If two columns look equally plausible for `date_var`, `target_var`, or `group_var`, ask the user rather than picking one silently.

## Step 3: Check the history is long enough

Using the sample rows and the date column's min/max from `tool_describe_dataset`:

- Fewer than **8** distinct dates: refuse politely — there is not enough history for a meaningful forecast. Explain what would help (more historical data).
- Fewer than **2** full seasonal cycles for the detected frequency (e.g. under 24 months of monthly data): warn the user the forecast may be unreliable, but proceed if they still want it.

## Step 4: Choose the horizon

A reasonable horizon is at most about half the length of the history. Default to 3 periods for a monthly series when the user does not specify one.

## Step 5: Create and embed the chart

Call `tool_create_forecast_chart(dataset_id, date_var, target_var, horizon, title, group_var, frequency)`, then `embed_chart(chart_id, title)` to show the widget in the conversation. Do not repeat the card in your reply.

## Step 6: Comment the result

Using the `summary` returned by `tool_create_forecast_chart`, comment in plain language:

- The **reliability badge** (good / fair / poor / unknown).
- Whether the model **beats the naive baseline** or not.
- The width of the confidence bands, in words: an 80% band contains roughly 8 out of 10 real values.

Never promise more than the numbers show. If reliability is poor or the model does not beat the naive baseline, say so plainly.

## Step 7: Offer to add it to the dashboard

When working inside a dashboard's chat, offer to add the chart with `tool_add_chart_to_dashboard`. Do not do it unasked outside that context.

## Rules

- Base the badge of fiabilité, real-world tracking, and any explained ruptures on what the tool actually returns — one factual sentence each, never speculation.
- Reply in the same language as the user.
