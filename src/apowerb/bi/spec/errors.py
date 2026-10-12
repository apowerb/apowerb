"""
bi/spec/errors.py
-----------------
Errors raised by the dashboard spec v2 (validation, patch operations, engine).
"""

from __future__ import annotations


class SpecError(ValueError):
    """A human-readable problem with a dashboard spec.

    ``errors`` keeps the individual messages when several problems were found
    at once (``validate_spec``); ``str(exc)`` joins them.
    """

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors) if errors else [message]
