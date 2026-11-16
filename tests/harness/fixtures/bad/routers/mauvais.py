"""Harness counter-example: every statement sends an exception's text to the client. Never imported."""
from fastapi import HTTPException


def leaks(call):
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except KeyError as e:
        return {"status": "error", "message": f"lookup failed: {e}"}
    except Exception as exc:
        return {"event": "error", "detail": f"{type(exc).__name__}: {exc}"}
