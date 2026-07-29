"""Tests for the disk storage helper used by the webhook handler.

Hostile inputs we explicitly exercise — these come from outside (email
attachment names are operator-controlled, the deep-link query string is
attacker-controlled):

  * filenames containing path separators ``/`` ``\\``
  * filenames containing ``..`` parent traversals
  * filenames containing NUL bytes
  * absolute paths embedded in the filename
  * a request to read a file that lives outside ATTACHMENT_ROOT

All of these must either be neutralised or refused.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest

from th2agent.storage import webhook_attachments as wa


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """Re-route ATTACHMENT_ROOT into pytest's tmp_path for isolation."""
    monkeypatch.setattr(wa, "ATTACHMENT_ROOT", tmp_path)
    return tmp_path


_FIXED_NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


class TestSafeFilename:
    def test_strips_directory_components(self):
        assert wa._safe_filename("/etc/passwd") == "passwd"

    def test_strips_windows_separators(self):
        # Linux basename() does not recognise backslashes; we replace
        # them with underscores so the result is still a safe leaf name
        # confined to the per-log directory.
        result = wa._safe_filename("c:\\windows\\system32\\hosts")
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_strips_parent_traversal(self):
        # Two-dot traversal is replaced, not just basenamed away.
        result = wa._safe_filename("..")
        assert ".." not in result

    def test_strips_nul_bytes(self):
        assert "\x00" not in wa._safe_filename("evil\x00.pdf")

    def test_empty_input_yields_fallback(self):
        assert wa._safe_filename("") == "attachment"

    def test_preserves_legit_filename(self):
        assert wa._safe_filename("Facture 2026-05.pdf") == "Facture 2026-05.pdf"


class TestStoreWebhookAttachment:
    def test_writes_under_year_month_logid(self, tmp_root):
        att = wa.store_webhook_attachment(
            log_id=42, filename="hello.pdf",
            content=b"%PDF-1.7\n...", now=_FIXED_NOW,
        )
        expected = tmp_root / "2026" / "05" / "42" / "hello.pdf"
        assert pathlib.Path(att.path) == expected
        assert expected.read_bytes() == b"%PDF-1.7\n..."
        assert att.size == len(b"%PDF-1.7\n...")
        assert att.content_type == "application/pdf"

    def test_explicit_content_type_overrides_guess(self, tmp_root):
        att = wa.store_webhook_attachment(
            log_id=1, filename="weird.xyz",
            content=b"x", content_type="application/x-magic",
            now=_FIXED_NOW,
        )
        assert att.content_type == "application/x-magic"

    def test_collision_gets_numeric_suffix(self, tmp_root):
        first = wa.store_webhook_attachment(
            log_id=7, filename="ar.pdf", content=b"A", now=_FIXED_NOW,
        )
        second = wa.store_webhook_attachment(
            log_id=7, filename="ar.pdf", content=b"B", now=_FIXED_NOW,
        )
        assert first.path != second.path
        assert pathlib.Path(first.path).read_bytes() == b"A"
        assert pathlib.Path(second.path).read_bytes() == b"B"
        # Suffix preserves extension.
        assert second.path.endswith("_1.pdf")

    def test_same_content_is_idempotent_no_duplicate(self, tmp_root):
        # A webhook log reprocessed N times (retrigger / Graph
        # re-notification) re-downloads the *same* bytes. We must reuse
        # the existing file, not pile up identical copies.
        results = [
            wa.store_webhook_attachment(
                log_id=7, filename="ar.pdf", content=b"SAME", now=_FIXED_NOW,
            )
            for _ in range(5)
        ]
        paths = {r.path for r in results}
        assert len(paths) == 1, "identical re-stores must collapse to one path"
        log_dir = tmp_root / "2026" / "05" / "7"
        assert sorted(p.name for p in log_dir.iterdir()) == ["ar.pdf"]
        # No spurious _1 suffix.
        assert not results[-1].path.endswith("_1.pdf")

    def test_hostile_filename_is_confined_to_log_dir(self, tmp_root):
        att = wa.store_webhook_attachment(
            log_id=99, filename="../../../../etc/passwd",
            content=b"root:x:0:0:", now=_FIXED_NOW,
        )
        # Resolved path MUST be inside the per-log dir.
        log_dir = (tmp_root / "2026" / "05" / "99").resolve()
        assert pathlib.Path(att.path).resolve().is_relative_to(log_dir)

    def test_to_jsonable_shape(self, tmp_root):
        att = wa.store_webhook_attachment(
            log_id=1, filename="x.pdf", content=b"x", now=_FIXED_NOW,
        )
        data = att.to_jsonable()
        assert set(data.keys()) == {"filename", "path", "content_type", "size"}


class TestResolveAttachmentPath:
    def test_finds_stored_attachment(self, tmp_root):
        wa.store_webhook_attachment(
            log_id=10, filename="ar.pdf", content=b"x", now=_FIXED_NOW,
        )
        resolved = wa.resolve_attachment_path(10, "ar.pdf", root=tmp_root)
        assert resolved.exists()
        assert resolved.read_bytes() == b"x"

    def test_missing_attachment_raises(self, tmp_root):
        with pytest.raises(ValueError, match="attachment not found"):
            wa.resolve_attachment_path(10, "absent.pdf", root=tmp_root)

    def test_traversal_in_filename_is_neutralised(self, tmp_root):
        # Even if the URL had ../../etc/passwd, the basename strip
        # turns it into "passwd" which is not in our store → not found.
        with pytest.raises(ValueError, match="attachment not found"):
            wa.resolve_attachment_path(10, "../../etc/passwd", root=tmp_root)

    def test_cannot_escape_root_via_symlink(self, tmp_root, tmp_path_factory):
        # Build a per-log directory that contains a symlink pointing
        # outside ATTACHMENT_ROOT, and prove resolve refuses it.
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret").write_bytes(b"top secret")
        log_dir = tmp_root / "2026" / "05" / "11"
        log_dir.mkdir(parents=True)
        (log_dir / "evil").symlink_to(outside / "secret")

        with pytest.raises(ValueError, match="path traversal"):
            wa.resolve_attachment_path(11, "evil", root=tmp_root)
