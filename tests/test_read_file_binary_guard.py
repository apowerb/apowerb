"""Regression tests for the binary guard in ``tool_read_file``.

Production trace: an agent handed a PDF attachment to ``tool_read_file``.
The old fallback chain listed ``latin-1``, which maps all 256 byte values
and therefore never raises, so the tool reported ``status="success"`` and
returned the file's bytes as pseudo-text. That text carried NUL characters;
PostgreSQL refuses them in ``text`` and ``jsonb`` columns, so persisting the
resulting event failed with ``UntranslatableCharacterError`` and the entire
run was lost behind an opaque 500.
"""

from __future__ import annotations

from apowerb.tools_store.portfolio.basic import tool_read_file

# Minimal real PDF header: the magic bytes plus the NUL-bearing binary that
# any PDF carries in its object streams.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Length 4 >>\nstream\n\x00\x01\x00\x02\nendstream\n"


def _write(tmp_path, name, data, *, mode="wb", **kwargs):
    target = tmp_path / name
    if mode == "wb":
        target.write_bytes(data)
    else:
        target.write_text(data, **kwargs)
    return str(target)


def test_binary_file_is_refused_instead_of_decoded(tmp_path):
    path = _write(tmp_path, "attachment.pdf", PDF_BYTES)

    result = tool_read_file(path)

    assert result["status"] == "error"
    assert "content" not in result
    assert "attachment.pdf" in result["error_message"]


def test_refusal_never_leaks_a_nul_to_the_caller(tmp_path):
    path = _write(tmp_path, "attachment.pdf", PDF_BYTES)

    result = tool_read_file(path)

    assert "\x00" not in "".join(str(v) for v in result.values())


def test_utf8_text_file_is_returned_unchanged(tmp_path):
    text = "Ligne 1\nDeuxieme ligne avec accents: e a u\n"
    path = _write(tmp_path, "note.txt", text, mode="w", encoding="utf-8")

    result = tool_read_file(path)

    assert result["status"] == "success"
    assert result["content"] == text
    assert result["encoding"] == "utf-8"
    assert result["line_count"] == 2


def test_empty_file_is_not_mistaken_for_binary(tmp_path):
    path = _write(tmp_path, "empty.txt", b"")

    result = tool_read_file(path)

    assert result["status"] == "success"
    assert result["content"] == ""


def test_nul_beyond_the_sniff_window_is_stripped_downstream(tmp_path):
    # The guard reads only the leading block, matching git and grep. A NUL
    # further in slips past it, which is exactly why ``to_jsonable`` strips
    # NULs again at the tool boundary before anything is persisted.
    from apowerb.helpers.jsonify import to_jsonable

    path = _write(tmp_path, "late.txt", b"a" * 9000 + b"\x00tail")

    result = tool_read_file(path)

    assert result["status"] == "success"
    assert "\x00" in result["content"]
    assert "\x00" not in to_jsonable(result)["content"]


def test_utf16_text_is_read_as_text_not_refused_as_binary(tmp_path):
    # UTF-16 is full of zero bytes. A NUL sniff alone would misfile it as
    # binary, and the old fallback chain mangled it into one character per
    # byte; the byte-order mark settles it.
    text = "Reference client\nDeuxieme ligne\n"
    path = tmp_path / "utf16.txt"
    path.write_bytes(text.encode("utf-16"))

    result = tool_read_file(str(path))

    assert result["status"] == "success"
    assert result["content"] == text
    assert "\x00" not in result["content"]


def test_latin1_fallback_order_is_unchanged(tmp_path):
    # The bytes 0x80-0x9f decode differently under cp1252 and latin-1.
    # latin-1 still wins, as it did before the binary guard was added, so
    # existing files keep decoding exactly the same way.
    path = tmp_path / "legacy.txt"
    path.write_bytes(b"caf\xe9 \x93quoted\x94")

    result = tool_read_file(str(path))

    assert result["status"] == "success"
    assert result["encoding"] == "latin-1"
    assert result["content"] == "caf\xe9 \x93quoted\x94"


def test_unreadable_file_is_not_reported_as_binary(tmp_path):
    # The sniff must not swallow a permission error and blame the format.
    path = tmp_path / "denied.txt"
    path.write_text("secret")
    path.chmod(0o000)
    try:
        result = tool_read_file(str(path))
    finally:
        path.chmod(0o644)

    assert result["status"] == "error"
    assert "not text" not in result["error_message"]


def test_utf16_le_is_read_as_text(tmp_path):
    text = "Ligne LE\n"
    path = tmp_path / "le.txt"
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))

    result = tool_read_file(str(path))

    assert result["status"] == "success"
    assert result["content"] == text


def test_utf16_be_is_read_as_text(tmp_path):
    text = "Ligne BE\n"
    path = tmp_path / "be.txt"
    path.write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))

    result = tool_read_file(str(path))

    assert result["status"] == "success"
    assert result["content"] == text


def test_utf8_text_containing_a_nul_is_now_refused(tmp_path):
    # Deliberate behaviour change: this input used to come back as
    # status="success", and its NUL then broke the caller's event write.
    # Refusing it here is the point of the guard.
    path = tmp_path / "odd.txt"
    path.write_bytes(b"A\x00B")

    result = tool_read_file(str(path))

    assert result["status"] == "error"
    assert "not text" in result["error_message"]
