"""Tests that the three download tools save attachments under the
``uploads/agent{id}/`` folder, matching the convention used by factory-bound
tools (``pdf_to_images``, ``read_uploaded_file``, ``create_downloadable_file``).

Without this alignment, an Outlook/OneDrive/Drive download lands in
``uploads/{id}/`` while ``tool_pdf_to_images`` looks under
``uploads/agent{id}/`` → silent "File not found" → the LLM hallucinates that
its vision tool failed.
"""
from __future__ import annotations

import inspect
import re

import pytest


# La racine passe désormais par ``configs.paths.uploads_dir()`` — le littéral
# ``"uploads"`` en dur est interdit par ``test_uploads_wiring.py``. Ce qui est
# vérifié ici reste le *nom du sous-dossier* : ``agent{id}``, pas ``{id}``.
# Deux formes acceptables :
#   str(uploads_dir() / f"agent{agent_id}")
#   agent_folder = f"agent{agent_id}"; str(uploads_dir() / agent_folder)
_AGENT_FOLDER_PATTERN = re.compile(
    r"""uploads_dir\(\)\s*/\s*"""
    r"""(?:f["']agent\{agent_id\}["']|agent_folder\b)"""
)


def _source_of(module_path: str) -> str:
    import importlib

    module = importlib.import_module(module_path)
    return inspect.getsource(module)


@pytest.mark.parametrize(
    "module_path,fn_name",
    [
        ("apowerb.tools_store.portfolio.outlook_mail", "tool_download_attachment"),
        ("apowerb.tools_store.portfolio.onedrive_read", "tool_download_file"),
        ("apowerb.tools_store.portfolio.google_drive", "tool_download_file"),
    ],
)
def test_download_tool_uses_agent_prefixed_folder(module_path, fn_name):
    src = _source_of(module_path)
    assert _AGENT_FOLDER_PATTERN.search(src), (
        f"{module_path}.{fn_name} must save under "
        "`uploads/agent{agent_id}/` to align with the agent_name "
        "convention used by pdf_to_images / read_uploaded_file."
    )

    bare_pattern = re.compile(r"""uploads_dir\(\)\s*/\s*agent_id\b""")
    assert not bare_pattern.search(src), (
        f"{module_path} still has the old `uploads/{{agent_id}}/` form — "
        "it produces a folder name like 'uploads/3/' instead of "
        "'uploads/agent3/' and breaks pdf_to_images downstream."
    )


def test_onedrive_s3_key_matches_local_folder():
    """S3 mode uses the same folder convention as local mode, otherwise
    files uploaded to S3 are unreachable from ``read_uploaded_file``."""
    src = _source_of("apowerb.tools_store.portfolio.onedrive_read")
    s3_key_old = re.compile(r"""s3_key\s*=\s*f["']uploads/\{agent_id\}/""")
    assert not s3_key_old.search(src), (
        "onedrive_read uploads to S3 with `uploads/{agent_id}/...` — "
        "must use `agent_folder` (= 'agent{id}') to match the local path."
    )


def test_outlook_download_save_dir(monkeypatch, tmp_path):
    """End-to-end check at the os.makedirs level — the dir actually created
    by the tool must end with ``/agentX``."""
    from apowerb.tools_store.portfolio import outlook_mail

    monkeypatch.setenv("ROOT_AGENT_ID", "42")
    monkeypatch.chdir(tmp_path)

    src = inspect.getsource(outlook_mail.tool_download_attachment)
    assert _AGENT_FOLDER_PATTERN.search(src), (
        "tool_download_attachment must build save_dir as "
        "`uploads_dir() / f'agent{agent_id}'` "
        "(or via an intermediate `agent_folder` variable)."
    )
