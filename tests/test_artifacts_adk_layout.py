"""The artifacts endpoints must read the layout ADK actually writes.

Captured from a real dev run on 2026-08-04, after an agent called
``tool_save_code_artifact`` twice:

    artifacts_store/users/<user>/sessions/<session>/artifacts/
        fizzbuzz.py/versions/0/fizzbuzz.py
        fizzbuzz.py/versions/0/metadata.json

The endpoints used to build ``<root>/<agent>/<user>/<session>`` and list it
flat. That directory never exists, so ``os.path.exists`` returned False and the
route answered ``[]`` — no error, no log, an empty screen for artifacts that
were sitting on disk. The agent name is absent from the real path entirely.
"""

import json
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

USER = "testt@gmail.com"
SESSION = "session_1785833154778"
AGENT = "agent12"


def _write_artifact(root, user, session, name, payload, version=0):
    d = os.path.join(
        root, "users", user, "sessions", session, "artifacts", name,
        "versions", str(version),
    )
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(json.dumps(payload))
    with open(os.path.join(d, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump({"version": version, "fileName": name}, f)


@pytest.fixture()
def env():
    root = tempfile.mkdtemp(prefix="artifacts_adk_")
    _write_artifact(root, USER, SESSION, "fizzbuzz.py", {
        "filename": "fizzbuzz.py", "language": "python", "code": "print(1)",
    })
    _write_artifact(root, USER, SESSION, "rapport.html", {
        "filename": "rapport.html", "language": "html", "code": "<h1>hi</h1>",
    })

    from apowerb.routers.artifacts import router
    from apowerb.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user():
        u = MagicMock()
        u.email = USER
        u.user_id = 1
        u.role = "USER"
        return u

    app.dependency_overrides[get_current_user] = _user

    with patch("apowerb.routers.artifacts.artifacts_store_dir", return_value=root):
        yield TestClient(app), root

    shutil.rmtree(root, ignore_errors=True)


def test_lists_the_artifacts_adk_wrote(env):
    client, _ = env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}")
    assert r.status_code == 200
    names = sorted(a["filename"] for a in r.json())
    assert names == ["fizzbuzz.py", "rapport.html"]


def test_carries_the_language_so_the_html_preview_can_trigger(env):
    client, _ = env
    by_name = {a["filename"]: a for a in client.get(
        f"/api/artifacts/{AGENT}/{USER}/{SESSION}").json()}
    assert by_name["rapport.html"]["language"] == "html"
    assert by_name["fizzbuzz.py"]["language"] == "python"


def test_reads_the_body_of_one_artifact(env):
    client, _ = env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/fizzbuzz.py")
    assert r.status_code == 200
    assert r.json()["code"] == "print(1)"
    assert r.json()["language"] == "python"


def test_serves_the_highest_version(env):
    client, root = env
    _write_artifact(root, USER, SESSION, "fizzbuzz.py", {
        "filename": "fizzbuzz.py", "language": "python", "code": "print(2)",
    }, version=1)

    listed = {a["filename"]: a for a in client.get(
        f"/api/artifacts/{AGENT}/{USER}/{SESSION}").json()}
    assert listed["fizzbuzz.py"]["version"] == 1

    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/fizzbuzz.py")
    assert r.json()["code"] == "print(2)"


def test_versions_sort_numerically_not_alphabetically(env):
    client, root = env
    for v in (2, 10):
        _write_artifact(root, USER, SESSION, "fizzbuzz.py", {
            "filename": "fizzbuzz.py", "language": "python",
            "code": f"print({v})",
        }, version=v)
    # "10" < "2" as strings — a lexical max would serve version 2.
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/fizzbuzz.py")
    assert r.json()["code"] == "print(10)"


def test_agent_name_is_not_part_of_the_real_path(env):
    client, _ = env
    # ADK scopes by user and session only. Any agent name must resolve to the
    # same artifacts rather than to an empty list.
    a = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}").json()
    b = client.get(f"/api/artifacts/whatever-agent/{USER}/{SESSION}").json()
    assert len(a) == len(b) == 2


def test_unknown_session_stays_empty_not_an_error(env):
    client, _ = env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/session_does_not_exist")
    assert r.status_code == 200
    assert r.json() == []


def test_missing_artifact_is_404(env):
    client, _ = env
    r = client.get(f"/api/artifacts/{AGENT}/{USER}/{SESSION}/nope.py")
    assert r.status_code == 404
