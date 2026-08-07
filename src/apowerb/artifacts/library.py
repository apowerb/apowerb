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
single object body.

**Two listings in total**, not two per agent. Listing each agent's prefix
separately cost 24 calls for an account owning 12 agents — 1 896 ms,
sequentially, for seven artifacts. Sweeping both roots costs 151 ms and
238 ms whatever the number of agents, and the ownership filter runs in
memory. That trade holds while the bucket stays small (470 objects here);
if it ever grows past the point where a full sweep is slower than N
targeted listings, this is the decision to revisit.
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


def _artifact_entries(owned: set[str]) -> dict[tuple, dict]:
    """Latest version of every artifact, for owned agents only.

    The sweep sees every agent in the bucket, so the ownership test is what
    keeps one user's files out of another's library. It is applied here,
    before anything is collected — never on the way out.

    ADK writes a ``metadata.json`` next to each artifact; both live under
    the same version folder, so the leaf is ignored and the artifact name
    comes from the path instead.
    """
    best: dict[tuple, dict] = {}
    root = f"{_ARTIFACTS_ROOT}/"

    for obj in list_objects_in_s3(prefix=root):
        parts = obj["key"][len(root):].split("/")
        # {agent}/{session}/{segment}/{filename…}/{version}/{leaf}
        if len(parts) < 6:
            continue
        agent, session, segment = parts[0], parts[1], parts[2]
        if agent not in owned or segment not in (INPUT, OUTPUT):
            continue
        version_raw = parts[-2]
        if not version_raw.isdigit():
            continue

        filename = "/".join(parts[3:-2])
        if not filename:
            continue

        identity = (agent, session, segment, filename)
        version = int(version_raw)
        current = best.get(identity)
        # Numeric comparison: "10" sorts before "2" as text.
        if current is None or version > current["version"]:
            best[identity] = _entry(agent, session, segment, filename, version, obj)

    return best


def _legacy_entries(owned: set[str], taken: dict[str, set[str]]) -> list[dict]:
    """Files under the pre-artifact layout, which carry no session.

    A name that also exists as a real artifact of the same agent is
    dropped: it is the same document, and showing both would read as two.
    """
    root = f"{_LEGACY_ROOT}/"
    entries = []

    for obj in list_objects_in_s3(prefix=root):
        parts = obj["key"][len(root):].split("/")
        # {agent}/{filename} — nothing deeper belongs to this layout.
        if len(parts) != 2:
            continue
        agent, name = parts
        if agent not in owned or not name or name in taken.get(agent, ()):
            continue
        entries.append(_entry(agent, "_shared", LEGACY, name, 0, obj))

    return entries


def build_library(agents: dict[str, str]) -> list[dict]:
    """Every artifact of every given agent, newest first.

    ``agents`` maps folder name ("agent12") to the name a human reads.
    """
    if not agents:
        return []

    owned = set(agents)
    by_identity = _artifact_entries(owned)

    names_by_agent: dict[str, set[str]] = {}
    for entry in by_identity.values():
        names_by_agent.setdefault(entry["agent_folder"], set()).add(entry["filename"])

    items = list(by_identity.values()) + _legacy_entries(owned, names_by_agent)
    for entry in items:
        entry["agent_name"] = agents[entry["agent_folder"]]

    items.sort(key=lambda e: (e["updated_at"] or 0), reverse=True)
    return items
