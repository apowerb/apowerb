"""Agent-bound file tools must find a file the agent was given by name in
S3 mode too.

``tool_pdf_to_images``, ``tool_pdf_first_page`` and the OneDrive
``tool_upload_file`` looked only in ``uploads/{agent}/`` on disk. With S3
configured, uploads land in ``artifacts/{agent}/…/input/…`` and
``create_downloadable_file`` writes to ``…/output/…``, so all three answered
"not found" for every file. They now go through ``artifacts.agent_uploads``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apowerb.artifacts import agent_uploads

fitz = pytest.importorskip("fitz")


def _pdf_bytes(*pages: str) -> bytes:
    doc = fitz.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def s3(monkeypatch):
    """S3 mode with an in-memory bucket for agent42."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(agent_uploads, "is_s3_storage", lambda: True)

    def read(folder, name):
        content = store.get(f"{folder}/{name}")
        if content is not None:
            return content, []
        return None, sorted(k.split("/", 1)[1] for k in store if k.startswith(folder + "/"))

    with patch("apowerb.artifacts.file_lookup.read_file_and_names", side_effect=read):
        yield store


class TestPdfFirstPageS3:
    def test_reads_upload_from_s3(self, s3):
        from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_first_page

        s3["agent42/ar.pdf"] = _pdf_bytes("AR CF101011", "page two")
        res = _make_pdf_first_page("agent42")("ar.pdf")

        assert res["status"] == "success", res
        assert "CF101011" in res["text"]
        assert res["total_pages_in_pdf"] == 2

    def test_missing_lists_s3_files(self, s3):
        from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_first_page

        s3["agent42/other.pdf"] = _pdf_bytes("x")
        res = _make_pdf_first_page("agent42")("ar.pdf")

        assert res["status"] == "error"
        assert "not found" in res["message"]
        assert res["available_files"] == ["other.pdf"]


class TestPdfToImagesS3:
    def test_renders_upload_from_s3(self, s3):
        pytest.importorskip("PIL")
        from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_to_images

        s3["agent42/scan.pdf"] = _pdf_bytes("one", "two", "three")
        res = _make_pdf_to_images("agent42")("scan.pdf", max_pages=2)

        assert res["status"] == "success", res
        assert res["page_count"] == 2
        assert res["total_pages_in_pdf"] == 3

    def test_path_is_refused(self, s3):
        from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_to_images

        s3["agent42/scan.pdf"] = _pdf_bytes("one")
        res = _make_pdf_to_images("agent42")("../agent7/scan.pdf")

        assert res["status"] == "error"
        assert "not found" in res["message"]


class TestOneDriveUploadS3:
    def test_uploads_file_created_in_s3(self, s3):
        from apowerb.core.agent_helpers.tool_factories import _make_upload_file

        s3["agent42/report.csv"] = b"a,b\n1,2\n"
        seen = {}

        def fake_upload(local_file_path, **kwargs):
            seen["name"] = local_file_path.rsplit("/", 1)[-1]
            with open(local_file_path, "rb") as fh:
                seen["bytes"] = fh.read()
            seen.update(kwargs)
            return {"status": "success"}

        with patch(
            "apowerb.tools_store.portfolio.onedrive_write.tool_upload_file",
            side_effect=fake_upload,
        ):
            res = _make_upload_file("agent42")(
                "report.csv", destination_path="Documents/report.csv"
            )

        assert res == {"status": "success"}
        # Same name and bytes reach OneDrive; the destination is passed through.
        assert seen["name"] == "report.csv"
        assert seen["bytes"] == b"a,b\n1,2\n"
        assert seen["destination_path"] == "Documents/report.csv"

    def test_missing_file_is_not_uploaded(self, s3):
        from apowerb.core.agent_helpers.tool_factories import _make_upload_file

        with patch("apowerb.tools_store.portfolio.onedrive_write.tool_upload_file") as up:
            res = _make_upload_file("agent42")("report.csv")

        assert res["status"] == "error"
        assert res["retry"] is False
        up.assert_not_called()


class TestLocalModeUnchanged:
    def test_upload_file_reads_disk(self, monkeypatch, tmp_path):
        from apowerb.core.agent_helpers.tool_factories import _make_upload_file

        monkeypatch.setattr(agent_uploads, "is_s3_storage", lambda: False)
        monkeypatch.setattr("apowerb.configs.paths.uploads_dir", lambda: tmp_path)
        (tmp_path / "agent42").mkdir()
        (tmp_path / "agent42" / "report.csv").write_bytes(b"x")

        with patch(
            "apowerb.tools_store.portfolio.onedrive_write.tool_upload_file",
            return_value={"status": "success"},
        ) as up:
            res = _make_upload_file("agent42")("report.csv")

        assert res == {"status": "success"}
        assert up.call_args.kwargs["local_file_path"] == str(
            (tmp_path / "agent42" / "report.csv").resolve()
        )
