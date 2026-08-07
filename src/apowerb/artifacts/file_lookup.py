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

from typing import Optional

from apowerb.storage.s3 import (
    download_file_from_s3,
    file_exists_in_s3,
    list_files_in_s3,
)

_ARTIFACTS_ROOT = "artifacts"
_LEGACY_PREFIX = "uploads/{agent}/"
INPUT = "input"
OUTPUT = "output"


def _latest_by_segment(agent: str, filename: str) -> dict[str, str]:
    """Latest key of ``filename`` per segment, from a single listing.

    One LIST over the agent's artifact prefix answers for both segments:
    asking per segment doubled the round trips for every file an agent
    reads, for the same bytes.

    Versions are compared as integers: sorting the raw keys would place
    "10" before "2" and serve a stale version.
    """
    prefix = f"{_ARTIFACTS_ROOT}/{agent}/"
    leaf_name = filename.split("/")[-1]
    best: dict[str, tuple[int, str]] = {}

    for key in list_files_in_s3(prefix=prefix):
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
            version = int(tail[0])
            if segment not in best or version > best[segment][0]:
                best[segment] = (version, key)
            break

    return {segment: key for segment, (_, key) in best.items()}


def find_artifact_key(agent: str, filename: str, segment: str = INPUT) -> Optional[str]:
    """Key of the latest version of ``filename``, or ``None``."""
    return _latest_by_segment(agent, filename).get(segment)


def resolve_file_key(agent: str, filename: str) -> Optional[str]:
    """Key holding ``filename`` for this agent, newest convention first.

    Inputs win over outputs on a name collision, mirroring what the reader
    expects: ``read_uploaded_file`` is asked about a file the *user* sent.
    """
    latest = _latest_by_segment(agent, filename)
    for segment in (INPUT, OUTPUT):
        if segment in latest:
            return latest[segment]

    legacy = _LEGACY_PREFIX.format(agent=agent) + filename
    return legacy if file_exists_in_s3(legacy) else None


def read_file_bytes(agent: str, filename: str) -> Optional[bytes]:
    key = resolve_file_key(agent, filename)
    return None if key is None else download_file_from_s3(key)


def available_filenames(agent: str) -> list[str]:
    """Every filename this agent can be asked about, across the three
    conventions -- what an agent gets told when it names a file that does
    not exist."""
    names: set[str] = set()

    artifacts_prefix = f"{_ARTIFACTS_ROOT}/{agent}/"
    for key in list_files_in_s3(prefix=artifacts_prefix):
        rest = key[len(artifacts_prefix):].split("/")
        # {session}/{segment}/{name}/{version}/{leaf}
        if len(rest) >= 5 and rest[1] in (INPUT, OUTPUT):
            names.add("/".join(rest[2:-2]))

    legacy_prefix = _LEGACY_PREFIX.format(agent=agent)
    for key in list_files_in_s3(prefix=legacy_prefix):
        short = key[len(legacy_prefix):]
        if short and "/" not in short:
            names.add(short)

    return sorted(names)
