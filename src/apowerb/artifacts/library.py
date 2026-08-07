"""Builds the whole artifact library from S3 keys alone.

The tab used to ask the API once per session, plus once per agent for the
shared scope. Measured on the dev bucket with a real account: 386 sessions
meant ~400 HTTP calls at ~200 ms each — six in flight, so roughly fifteen
seconds before anything appeared, and that floor held even for sessions
holding nothing.

Every field the screen shows is already in the key:

    artifacts/{agent}/{session}/{segment}/{filename}/{version}/{leaf}
    uploads/{agent}/{filename}                                (legacy)

Agent, session, input-or-output, name and version all come from the path;
the language comes from the extension; the date and size come from the
listing response itself. So the library is built without downloading a
single object body — one listing per agent instead of three per session.
"""

from __future__ import annotations

from apowerb.artifacts.languages import language_for_filename
from apowerb.storage.s3 import list_objects_in_s3

_ARTIFACTS_ROOT = "artifacts"
_LEGACY_ROOT = "uploads"
INPUT = "input"
OUTPUT = "output"
LEGACY = "legacy"

_SOURCE_BY_KIND = {INPUT: "upload", OUTPUT: "adk", LEGACY: "legacy"}


def _entry(agent: str, session: str, kind: str, filename: str, version: int,
           obj: dict) -> dict:
    return {
        "agent_folder": agent,
        "session_id": session,
        "kind": kind,
        "filename": filename,
        "language": language_for_filename(filename),
        "version": version,
        "source": _SOURCE_BY_KIND[kind],
        "updated_at": obj["last_modified"].timestamp() if obj.get("last_modified") else None,
        "size": obj.get("size", 0),
    }


def _artifact_entries(agent: str) -> dict[tuple, dict]:
    """Latest version of every artifact of one agent, keyed by identity.

    ADK writes a ``metadata.json`` next to each artifact; both live under
    the same version folder, so the leaf is skipped and the artifact name
    is taken from the path instead.
    """
    best: dict[tuple, dict] = {}
    prefix = f"{_ARTIFACTS_ROOT}/{agent}/"

    for obj in list_objects_in_s3(prefix=prefix):
        parts = obj["key"][len(prefix):].split("/")
        # {session}/{segment}/{filename…}/{version}/{leaf}
        if len(parts) < 5:
            continue
        session, segment = parts[0], parts[1]
        if segment not in (INPUT, OUTPUT):
            continue
        version_raw = parts[-2]
        if not version_raw.isdigit():
            continue

        filename = "/".join(parts[2:-2])
        if not filename:
            continue

        identity = (agent, session, segment, filename)
        version = int(version_raw)
        current = best.get(identity)
        # Numeric comparison: "10" sorts before "2" as text.
        if current is None or version > current["version"]:
            best[identity] = _entry(agent, session, segment, filename, version, obj)

    return best


def _legacy_entries(agent: str, taken: set[str]) -> list[dict]:
    """Files under the pre-artifact layout, which carry no session.

    A name that also exists as a real artifact is dropped: it is the same
    document, and showing both would read as two.
    """
    prefix = f"{_LEGACY_ROOT}/{agent}/"
    entries = []

    for obj in list_objects_in_s3(prefix=prefix):
        name = obj["key"][len(prefix):]
        if not name or "/" in name or name in taken:
            continue
        entries.append(_entry(agent, "_shared", LEGACY, name, 0, obj))

    return entries


def build_library(agents: dict[str, str]) -> list[dict]:
    """Every artifact of every given agent, newest first.

    ``agents`` maps folder name ("agent12") to the name a human reads.
    """
    items: list[dict] = []

    for folder, display_name in agents.items():
        by_identity = _artifact_entries(folder)
        names = {e["filename"] for e in by_identity.values()}
        for entry in list(by_identity.values()) + _legacy_entries(folder, names):
            entry["agent_name"] = display_name
            items.append(entry)

    items.sort(key=lambda e: (e["updated_at"] or 0), reverse=True)
    return items
