"""Disk storage helper for email attachments captured at webhook time.

Layout
------
``/home/ubuntu/scei_webhook_attachments/<YYYY>/<MM>/<log_id>/<safe_filename>``

Design notes
------------
- Filenames coming from email attachments are *operator-controlled* (the
  sender can name a PJ ``../../etc/passwd``). ``_safe_filename`` strips
  path separators, parent traversals, and NUL bytes — what hits disk is
  always a leaf name inside the per-log directory.
- The retention policy is "forever, until S3 lands". A `df -h` alert is
  the only governor in place today (cf incident 2026-05-19 reviewer
  challenge). Do not add a cleanup cron here without a separate decision.
- This module is the single write site. The Graph fetch lives in
  ``OutlookWebhookService``; the orchestration (call fetch, call store,
  update DB row) lives in the outlook webhook handler. Keeping this
  module narrow makes it cheap to swap for an S3/OneDrive backend later.
"""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from th2agent.storage.filename import sanitize_filename


# Webhook attachment storage root. Env-driven (TH2_ATTACHMENT_ROOT) so the
# core stays client-agnostic. The legacy default preserves the existing
# prod location (the cron pj_archiver relies on it) — deployments override
# it via env. When S3 arrives, replace here.
ATTACHMENT_ROOT: Final[Path] = Path(
    os.environ.get("TH2_ATTACHMENT_ROOT", "/home/ubuntu/scei_webhook_attachments")
)


@dataclass(frozen=True)
class StoredAttachment:
    """Metadata persisted in ``webhook_logs.attachments`` JSONB."""

    filename: str
    path: str
    content_type: str
    size: int

    def to_jsonable(self) -> dict:
        return {
            "filename": self.filename,
            "path": self.path,
            "content_type": self.content_type,
            "size": self.size,
        }


def _safe_filename(raw: str) -> str:
    """Backwards-compatible alias for :func:`sanitize_filename`.

    Kept so legacy import sites and tests inside this module continue
    to work; new code should call :func:`sanitize_filename` directly.
    """
    return sanitize_filename(raw)


def _target_dir(log_id: int, *, now: datetime | None = None) -> Path:
    """Where attachments of a given webhook_logs row land."""
    ts = now or datetime.now(timezone.utc)
    return ATTACHMENT_ROOT / f"{ts:%Y}" / f"{ts:%m}" / str(log_id)


def store_webhook_attachment(
    log_id: int,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    *,
    now: datetime | None = None,
) -> StoredAttachment:
    """Persist one attachment to disk and return its metadata.

    The directory is created on demand. Idempotent on identical content:
    if a file with the same safe name already holds the **same bytes**
    (the common case when a webhook log is reprocessed — retrigger or a
    Graph re-notification), we reuse it instead of writing a numbered
    copy. Only a file with the same name but *different* content still
    gets a ``_1``, ``_2`` … suffix, so two genuinely distinct PJ sharing
    a name don't overwrite each other. (Before this guard a reprocessed
    log piled up one copy per run — e.g. 176 identical PDFs in one dir.)
    """
    safe = _safe_filename(filename)
    target_dir = _target_dir(log_id, now=now)
    target_dir.mkdir(parents=True, exist_ok=True)

    def _stored(path: Path, size: int) -> StoredAttachment:
        mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return StoredAttachment(
            filename=path.name, path=str(path), content_type=mime, size=size,
        )

    candidate = target_dir / safe
    i = 1
    while candidate.exists():
        # Same name + same bytes already on disk -> idempotent reuse.
        if candidate.stat().st_size == len(content) and candidate.read_bytes() == content:
            return _stored(candidate, candidate.stat().st_size)
        stem, dot, ext = safe.rpartition(".")
        if dot:
            candidate = target_dir / f"{stem}_{i}.{ext}"
        else:
            candidate = target_dir / f"{safe}_{i}"
        i += 1

    candidate.write_bytes(content)
    return _stored(candidate, len(content))


def resolve_attachment_path(log_id: int, filename: str, root: Path | None = None) -> Path:
    """Resolve a request like ``GET /logs/<id>/attachments/<filename>`` to
    a real filesystem path, after verifying it does not escape the
    per-log directory.

    Raises ``ValueError`` on any traversal attempt or unknown file.
    Caller is responsible for checking that the ``log_id`` belongs to
    the current user before calling this helper.
    """
    base_root = root or ATTACHMENT_ROOT
    safe = _safe_filename(filename)

    # The file may be in any year/month directory under
    # ``<root>/<YYYY>/<MM>/<log_id>/``. The created_at lives on the row;
    # for simplicity we walk the year/month directories. The expected
    # depth is shallow (~12 month dirs/year) and only used by an
    # authenticated endpoint.
    base_root_resolved = base_root.resolve()
    for year_dir in base_root.glob("*"):
        if not year_dir.is_dir():
            continue
        for month_dir in year_dir.glob("*"):
            if not month_dir.is_dir():
                continue
            candidate = (month_dir / str(log_id) / safe)
            if not candidate.exists():
                continue
            resolved = candidate.resolve()
            # Defence-in-depth: ensure the resolved path is still
            # inside ATTACHMENT_ROOT.
            try:
                resolved.relative_to(base_root_resolved)
            except ValueError as exc:
                raise ValueError(
                    f"path traversal detected for log {log_id} / {filename!r}"
                ) from exc
            return resolved

    raise ValueError(f"attachment not found: log {log_id} / {filename!r}")
