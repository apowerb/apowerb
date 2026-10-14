"""
bi/spec/errors.py
-----------------
Errors raised by the dashboard spec v2 (validation, patch operations, engine).
"""

from __future__ import annotations


class SpecError(ValueError):
    """A human-readable problem with a dashboard spec.

    ``errors`` keeps the individual messages when several problems were found
    at once (``validate_spec``); ``str(exc)`` joins them. ``en`` is the English
    message of an error a dashboard user can see; ``text(lang)`` picks the one
    of the dashboard language (French by default).
    """

    def __init__(self, message: str, errors: list[str] | None = None, *, en: str | None = None) -> None:
        super().__init__(message)
        self.errors: list[str] = list(errors) if errors else [message]
        self.en = en

    def text(self, lang: str | None) -> str:
        return self.en if lang == "en" and self.en else str(self)

    def messages(self, lang: str | None) -> list[str]:
        return [self.en] if lang == "en" and self.en else self.errors
