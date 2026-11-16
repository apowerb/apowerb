# Exception text never reaches an HTTP client

> Use when a router, an SSE stream or a WebSocket turns a caught exception into what the client receives (`detail`, `message`, `error`).

`str(exc)` of a driver or library error can carry a DSN, an internal host, a file path or a provider traceback. CodeQL reports it as `py/stack-trace-exposure`; it has been fixed three times here (PRs #40 and #46, alert #76).

## Don't

```python
except Exception as exc:
    raise HTTPException(status_code=500, detail=str(exc))
    yield _sse({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
```

## Do

- **HTTP response**: log the exception, send a fixed message.
  ```python
  from apowerb.helpers.error_responses import safe_error_message

  except Exception as exc:
      raise HTTPException(
          status_code=500,
          detail=safe_error_message(
              exc, logger=logger, context="data-lake list", client_message="Could not list the files."
          ),
      ) from exc
  ```
- **Workflow stream** (`routers/workflows.py`, `core/workflow_*`): `{"event": "error", **workflow_engine.error_fields(exc)}`. An unexpected error goes out as `code: internal` with a `ref`; the text is logged once under that `ref`.
- **A message you wrote for the user** (validation, quota, "agent not found"): raise a typed error (`WorkflowUserError`, `GraphError`, an `HTTPException` with a literal `detail`). `error_fields` shows those as they are. Never pass a third-party exception's text through one.
- **Other consumers read the envelope**: before replacing a text, check who parses it (a retry that looks for `429`, a front that matches "session not found") and give them an explicit `code`.

## Check

- `python3 scripts/harness.py` — rule `no-exception-text-to-client` (ratchet): no new occurrence in `routers/` or `core/workflow_*`; clean a file, then `--update-baseline`.
- Test the guarantee, not the type: raise an exception whose text names a fake internal host, assert the host is absent from the response body and from the stored record, and present in `caplog` next to the `ref`.
