"""Finds a file on S3 whatever convention wrote it.

Three conventions coexist in the bucket, and a file written under one of
them used to be invisible to every reader of the others:

    artifacts/{agent}/{session}/input/{name}/{version}/{name}   uploads (#30)
    artifacts/{agent}/{session}/output/{name}/{version}/{name}  generated
    uploads/{agent}/{name}                                      legacy

Nothing writes to ``uploads/`` any more, but 455 objects sit there on the
dev bucket alone, and both ``read_uploaded_file`` and the download route
still pointed at it exclusively -- so a document attached to a conversation
was stored correctly and then reported "not found" by the agent one turn
later.

Resolution scans the artifact prefixes rather than taking a session id:
the agent only ever knows a filename, and the tools that call this have no
session in scope. One LIST over ``artifacts/{agent}/`` covers every session
of that agent, including the ``_shared`` scope of unsessioned uploads.
"""

from __future__ import annotations

import datetime
from typing import Optional

from apowerb.storage.s3 import (
    download_file_from_s3,
    file_exists_in_s3,
    list_files_in_s3,
    list_objects_in_s3,
)

_ARTIFACTS_ROOT = "artifacts"
_LEGACY_PREFIX = "uploads/{agent}/"
INPUT = "input"
OUTPUT = "output"


def _latest_by_segment(objects: list[dict], filename: str) -> dict[str, str]:
    """Latest key of ``filename`` per segment, from a single listing.

    One LIST over the agent's artifact prefix answers for both segments:
    asking per segment doubled the round trips for every file an agent
    reads, for the same bytes.

    The newest object wins. Version numbers only order the copies of one
    session -- each session numbers its uploads from 0 -- so across sessions
    they say nothing: a file attached again in a new conversation is v0
    like the one it replaces, and the first key listed used to win, serving
    the older upload. Versions still break a timestamp tie, compared as
    integers: sorting the raw keys would place "10" before "2".
    """
    leaf_name = filename.split("/")[-1]
    best: dict[str, tuple[tuple[datetime.datetime, int], str]] = {}
    oldest = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    for obj in objects:
        key = obj["key"]
        for segment in (INPUT, OUTPUT):
            marker = f"/{segment}/{filename}/"
            if marker not in key:
                continue
            # ...{segment}/{filename}/{version}/{leaf}
            tail = key.split(marker, 1)[1].split("/")
            if len(tail) != 2 or not tail[0].isdigit():
                break
            if tail[1] != leaf_name and tail[1] != filename:
                # metadata.json and other siblings are not the artifact.
                break
            rank = (obj.get("last_modified") or oldest, int(tail[0]))
            if segment not in best or rank > best[segment][0]:
                best[segment] = (rank, key)
            break

    return {segment: key for segment, (_, key) in best.items()}


def _artifact_objects(agent: str) -> list[dict]:
    return list_objects_in_s3(prefix=f"{_ARTIFACTS_ROOT}/{agent}/")


def _pick(latest: dict[str, str]) -> Optional[str]:
    """Inputs win over outputs on a name collision, mirroring what the reader
    expects: ``read_uploaded_file`` is asked about a file the *user* sent."""
    for segment in (INPUT, OUTPUT):
        if segment in latest:
            return latest[segment]
    return None


def _artifact_names(agent: str, keys) -> set[str]:
    prefix = f"{_ARTIFACTS_ROOT}/{agent}/"
    names: set[str] = set()
    for key in keys:
        rest = key[len(prefix):].split("/")
        # {session}/{segment}/{name}/{version}/{leaf}
        if len(rest) >= 5 and rest[1] in (INPUT, OUTPUT):
            names.add("/".join(rest[2:-2]))
    return names


def _legacy_names(agent: str, keys) -> set[str]:
    prefix = _LEGACY_PREFIX.format(agent=agent)
    return {
        short
        for short in (key[len(prefix):] for key in keys)
        if short and "/" not in short
    }


def find_artifact_key(agent: str, filename: str, segment: str = INPUT) -> Optional[str]:
    """Key of the latest version of ``filename``, or ``None``."""
    return _latest_by_segment(_artifact_objects(agent), filename).get(segment)


def resolve_file_key(agent: str, filename: str) -> Optional[str]:
    """Key holding ``filename`` for this agent, newest convention first."""
    key = _pick(_latest_by_segment(_artifact_objects(agent), filename))
    if key is not None:
        return key

    legacy = _LEGACY_PREFIX.format(agent=agent) + filename
    return legacy if file_exists_in_s3(legacy) else None


def read_file_bytes(agent: str, filename: str) -> Optional[bytes]:
    key = resolve_file_key(agent, filename)
    return None if key is None else download_file_from_s3(key)


def read_file_and_names(agent: str, filename: str) -> tuple[Optional[bytes], list[str]]:
    """``(bytes, [])`` for ``filename``, or ``(None, available_names)``.

    What a tool asked for a file needs: the bytes, or what to suggest
    instead. Calling ``read_file_bytes`` then ``available_filenames`` listed
    ``artifacts/{agent}/`` twice and ``uploads/{agent}/`` twice for every
    missing file; both answers come from one listing of each prefix here.
    """
    objects = _artifact_objects(agent)
    key = _pick(_latest_by_segment(objects, filename))
    if key is not None:
        return download_file_from_s3(key), []

    legacy_keys = list_files_in_s3(prefix=_LEGACY_PREFIX.format(agent=agent))
    legacy = _LEGACY_PREFIX.format(agent=agent) + filename
    # Exact membership, as file_exists_in_s3: report.html.bak is not report.html.
    if legacy in legacy_keys:
        return download_file_from_s3(legacy), []

    names = _artifact_names(agent, (obj["key"] for obj in objects))
    return None, sorted(names | _legacy_names(agent, legacy_keys))


def available_filenames(agent: str) -> list[str]:
    """Every filename this agent can be asked about, across the three
    conventions -- what an agent gets told when it names a file that does
    not exist."""
    names = _artifact_names(agent, list_files_in_s3(prefix=f"{_ARTIFACTS_ROOT}/{agent}/"))
    legacy = list_files_in_s3(prefix=_LEGACY_PREFIX.format(agent=agent))
    return sorted(names | _legacy_names(agent, legacy))
