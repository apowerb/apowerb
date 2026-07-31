"""Scoped mutations of ``os.environ`` for cross-tenant safe code paths.

Context
-------
Several legacy tools read their OAuth credentials from process-wide
environment variables (``OUTLOOK_REFRESH_TOKEN``, ``ONEDRIVE_REFRESH_TOKEN``,
``GOOGLE_DRIVE_REFRESH_TOKEN`` …). On a multi-tenant server two concurrent
requests from two different users were able to observe each other's
credentials before the variables were overwritten — see
``review-security.md`` Critical C6.

This module exposes an ``env_scope`` asynchronous context manager that:

1. Serializes concurrent invocations under an optional ``asyncio.Lock``
   (one per protected call-site).
2. Sets the given env vars for the duration of the scope.
3. **Always** restores the original state in ``finally`` — including on
   exception — so no credential ever leaks past the scope.

Usage
-----
.. code-block:: python

    from apowerb.helpers.env_scope import env_scope

    _google_drive_lock = asyncio.Lock()

    async with env_scope(
        {"GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token},
        lock=_google_drive_lock,
    ):
        return await asyncio.to_thread(tool_list_files, folder_id=folder_id)
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Mapping


@asynccontextmanager
async def env_scope(
    vars_: Mapping[str, str | None],
    *,
    lock: asyncio.Lock | None = None,
) -> AsyncIterator[None]:
    """Set env vars for the duration of the scope, restore on exit.

    Parameters
    ----------
    vars_
        Mapping of env var names to values. Entries with a ``None`` value
        are ignored (no env var is set) — convenient for optional creds.
    lock
        Optional ``asyncio.Lock`` used to serialize concurrent scopes
        touching the same set of variables. Two call-sites using the
        same lock are guaranteed to never observe each other's values.

    Yields
    ------
    None
        Control is yielded to the protected block. Any exception raised
        inside the block is propagated after the env vars have been
        restored.
    """
    if lock is not None:
        await lock.acquire()
    try:
        # Snapshot only the keys we are about to mutate. Restoring is
        # cheaper and safer than copying the full ``os.environ``.
        snapshot: dict[str, str | None] = {k: os.environ.get(k) for k in vars_}
        try:
            for k, v in vars_.items():
                if v is None:
                    continue
                os.environ[k] = v
            yield
        finally:
            for k, original in snapshot.items():
                if original is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = original
    finally:
        if lock is not None:
            lock.release()
