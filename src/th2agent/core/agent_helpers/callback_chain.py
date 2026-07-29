"""Chaînage de callbacks ADK ``after_model`` — utilitaire générique du noyau.

Extrait de ``usage_recorder.py`` quand la comptabilisation des jetons est partie
en brique commerciale. Chaîner deux callbacks n'est pas une fonctionnalité
vendue : c'est de la plomberie ADK, dont un observateur quelconque a besoin
autant que le noyau. Elle reste donc ici.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

AfterModelCallback = Callable[..., Any]


def chain_after_model_callbacks(
    recorder_cb: AfterModelCallback,
    existing_cb: Optional[AfterModelCallback],
) -> AfterModelCallback:
    """Chain an observer (always runs first, never returns a value) with an
    optional pre-existing ``after_model_callback`` (e.g. guardrails).

    The observer's return value is discarded; the chained callback returns
    whatever ``existing_cb`` returns (sync or async).
    """
    if existing_cb is None:
        return recorder_cb

    async def _chained(*, callback_context, llm_response):
        await recorder_cb(callback_context=callback_context, llm_response=llm_response)
        result = existing_cb(callback_context=callback_context, llm_response=llm_response)
        if inspect.isawaitable(result):
            result = await result
        return result

    return _chained
