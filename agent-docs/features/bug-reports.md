# Bug reports — model feature page

> Reference page for migration by contagion: document other features in this format.

## Purpose
A user reports a defect from the app with what it takes to reproduce it; identical defects group into one issue instead of twenty.

## Where the code is
- `src/apowerb/bug_reports/` — `areas.py` (feature concerned), `fingerprint.py` (grouping), `issue_body.py` (issue rendering), `redaction.py`, `screenshot.py`, `log_buffer.py`, `tracking.py`, `service.py`, `sinks/`
- Models: `BugReport`, `BugReportEvent` in `src/apowerb/models.py`
- Tests: `tests/` (search `bug_report`)

## Business rules
1. The area is a closed list (`BugArea`), pre-filled from the route and left editable: the route is usually right, the user sometimes sees what the route ignores (`areas.py`).
2. An unknown area value falls back to its raw identifier in the UI: visible, not broken (`areas.py` docstring).
3. Grouping fingerprint = normalised route + HTTP status + normalised error signature; never the user's description, which two people never phrase alike (`fingerprint.py`).
4. Route normalisation replaces integers, UUIDs, long hex and product identifiers, so `/api/agents/42/runs` and `/api/agents/77/runs` group together (`fingerprint.py`).
5. A 500 and a 403 on the same route are two defects, not one (`fingerprint.py`).

## States and transitions
`BugReportEvent` records what happened to a report, in order. The list of event types: see `tracking.py` (not summarised here yet).

## Functional edge cases
- Handled: same defect across many users (grouping), varying ids in routes.
- Open: a defect whose error message changes with the language of the UI — does it still group? (not checked)

## Checks
`DB_SCHEMA="" uv run pytest -q -m "not integration" -k bug_report`
