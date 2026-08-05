"""ADK artifact service backed by S3-compatible object storage.

ADK ships exactly three artifact backends: ``file://`` (local disk),
``gs://`` (Google Cloud Storage) and an in-memory service. There is no S3
backend, and Farid wants a single artifact store, on S3, for both dev and
prod (S3 is already configured in both — see ``apowerb.storage.s3``). This
mirrors ``google.adk.artifacts.gcs_artifact_service.GcsArtifactService``:
same method shapes, same ``asyncio.to_thread`` wrapping of blocking S3 calls,
same "no versions -> None" semantics.

Key layout is imposed by product, not copied from GCS:

    artifacts/{app_name}/{session_id}/output/{filename}/{version}/{filename}

``output`` distinguishes generated artifacts from their ``input`` sibling
(uploads, routers/files.py) — same key scheme, same S3 primitives, "input"
swapped in for "output" via the private ``segment`` parameter threaded
through ``_key``/``_key_prefix``/``_list_versions``. See the
``save_input_artifact``/``load_input_artifact``/``list_input_artifact_filenames``
methods below: no ADK ``types.Part`` wrapping (uploads are raw bytes) and no
``user_id``/``"user:"`` namespace (input artifacts are always scoped by
``(app_name, session_id)``, ``session_id`` being a real session or the
literal ``"_shared"`` scope — see ``apowerb.artifacts.input_scope``).

GCS's own scheme for user-scoped artifacts (filename starting with
``user:``, no session) swaps ``session_id`` for the literal ``"user"``
while keeping ``user_id`` in the path:
``{app_name}/{user_id}/user/{filename}/{version}``. The session-scoped
layout imposed above has no ``user_id`` segment at all, so there is nothing
to reuse for uniqueness in the user-scoped case: ``user_id`` becomes the
only available identifier, and takes the ``session_id`` slot instead:

    artifacts/{app_name}/user/{user_id}/output/{filename}/{version}/{filename}
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Union

from botocore.exceptions import ClientError
from google.adk.artifacts.base_artifact_service import (
    ArtifactVersion,
    BaseArtifactService,
    ensure_part,
)
from google.adk.errors.input_validation_error import InputValidationError
from google.genai import types
from typing_extensions import override

from apowerb.configs.settings import get_settings
from apowerb.storage.s3 import (
    delete_file_from_s3,
    get_object_with_metadata,
    list_files_in_s3,
    upload_bytes_to_s3,
)

# HeadObject is not used here: verified against the real dev bucket
# (th2agent-dev, OVH-hosted, SigV4) that boto3's HeadObject returns a bare
# "400 Bad Request" on every key, including a key freshly written by this
# same client — GetObject against the identical key succeeds. GetObject is
# used for metadata reads too (content type, custom metadata, last-modified
# all ride along in its response), at the cost of downloading the body when
# only metadata is needed (list_artifact_versions / get_artifact_version).
# Artifact payloads here are code/report snippets, not large blobs, so that
# cost is accepted rather than depending on an operation proven broken
# against the target infra.

logger = logging.getLogger("apowerb.artifacts.s3_artifact_service")

_ARTIFACTS_ROOT = "artifacts"
_OUTPUT_SEGMENT = "output"
_INPUT_SEGMENT = "input"
_USER_NAMESPACE_PREFIX = "user:"


def _not_found(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in (
        "404",
        "NoSuchKey",
        "NotFound",
    )


class S3ArtifactService(BaseArtifactService):
    """An artifact service implementation using S3-compatible storage."""

    def __init__(self) -> None:
        self._bucket_name = get_settings().s3_bucket_name

    # -- BaseArtifactService: async API ------------------------------------

    @override
    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        session_id: Optional[str] = None,
        custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        return await asyncio.to_thread(
            self._save_artifact,
            app_name,
            user_id,
            session_id,
            filename,
            artifact,
            custom_metadata,
        )

    @override
    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[types.Part]:
        return await asyncio.to_thread(
            self._load_artifact, app_name, user_id, session_id, filename, version
        )

    @override
    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: Optional[str] = None
    ) -> list[str]:
        return await asyncio.to_thread(
            self._list_artifact_keys, app_name, user_id, session_id
        )

    @override
    async def delete_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> None:
        return await asyncio.to_thread(
            self._delete_artifact, app_name, user_id, session_id, filename
        )

    @override
    async def list_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[int]:
        return await asyncio.to_thread(
            self._list_versions, app_name, user_id, session_id, filename
        )

    @override
    async def list_artifact_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[ArtifactVersion]:
        return await asyncio.to_thread(
            self._list_artifact_versions, app_name, user_id, session_id, filename
        )

    @override
    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[ArtifactVersion]:
        return await asyncio.to_thread(
            self._get_artifact_version, app_name, user_id, session_id, filename, version
        )

    # -- key layout ----------------------------------------------------------

    @staticmethod
    def _has_user_namespace(filename: str) -> bool:
        return filename.startswith(_USER_NAMESPACE_PREFIX)

    def _key_prefix(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str],
        segment: str = _OUTPUT_SEGMENT,
    ) -> str:
        if self._has_user_namespace(filename):
            return f"{_ARTIFACTS_ROOT}/{app_name}/user/{user_id}/{segment}/{filename}"
        if session_id is None:
            raise InputValidationError(
                "Session ID must be provided for session-scoped artifacts."
            )
        return f"{_ARTIFACTS_ROOT}/{app_name}/{session_id}/{segment}/{filename}"

    def _key(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        version: int,
        session_id: Optional[str],
        segment: str = _OUTPUT_SEGMENT,
    ) -> str:
        prefix = self._key_prefix(app_name, user_id, filename, session_id, segment)
        return f"{prefix}/{version}/{filename}"

    # -- sync implementations, run via asyncio.to_thread ---------------------

    def _save_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        custom_metadata: Optional[dict[str, Any]],
    ) -> int:
        artifact = ensure_part(artifact)
        versions = self._list_versions(app_name, user_id, session_id, filename)
        version = 0 if not versions else max(versions) + 1

        if artifact.inline_data:
            data = artifact.inline_data.data
            content_type = artifact.inline_data.mime_type or "application/octet-stream"
        elif artifact.text:
            data = artifact.text.encode("utf-8")
            content_type = "text/plain"
        elif artifact.file_data:
            raise NotImplementedError(
                "Saving artifact with file_data is not supported yet in"
                " S3ArtifactService."
            )
        else:
            raise InputValidationError(
                "Artifact must have either inline_data or text."
            )

        metadata = (
            {k: str(v) for k, v in custom_metadata.items()} if custom_metadata else None
        )
        key = self._key(app_name, user_id, filename, version, session_id)
        upload_bytes_to_s3(data, key, content_type=content_type, metadata=metadata)
        return version

    def _load_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        version: Optional[int],
    ) -> Optional[types.Part]:
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                return None
            version = max(versions)

        key = self._key(app_name, user_id, filename, version, session_id)
        try:
            obj = get_object_with_metadata(key)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        if not obj["body"]:
            return None

        return types.Part.from_bytes(data=obj["body"], mime_type=obj.get("content_type"))

    def _list_artifact_keys(
        self, app_name: str, user_id: str, session_id: Optional[str]
    ) -> list[str]:
        filenames: set[str] = set()
        if session_id:
            session_prefix = f"{_ARTIFACTS_ROOT}/{app_name}/{session_id}/{_OUTPUT_SEGMENT}/"
            filenames |= self._filenames_under(session_prefix)

        user_prefix = f"{_ARTIFACTS_ROOT}/{app_name}/user/{user_id}/{_OUTPUT_SEGMENT}/"
        filenames |= self._filenames_under(user_prefix)

        return sorted(filenames)

    @staticmethod
    def _filenames_under(prefix: str) -> set[str]:
        """Extracts artifact filenames from keys under a scope prefix.

        Each key looks like ``{prefix}{filename}/{version}/{leaf}`` — leaf is
        either the artifact content (basename of filename) or a sibling
        ``metadata.json``, neither of which matters here: dropping the last
        two path segments recovers the filename regardless of which leaf
        produced the key, and regardless of filename containing "/" itself.
        """
        filenames: set[str] = set()
        for key in list_files_in_s3(prefix=prefix):
            rest = key[len(prefix):]
            parts = rest.split("/")
            if len(parts) < 3:
                continue
            filenames.add("/".join(parts[:-2]))
        return filenames

    def _delete_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> None:
        prefix = self._key_prefix(app_name, user_id, filename, session_id)
        for version in self._list_versions(app_name, user_id, session_id, filename):
            for key in list_files_in_s3(prefix=f"{prefix}/{version}/"):
                delete_file_from_s3(key)

    def _list_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        segment: str = _OUTPUT_SEGMENT,
    ) -> list[int]:
        """Lists versions of an artifact.

        Versions are parsed to ``int`` before sorting/comparing — sorting the
        raw key strings would put "10" before "2" (lexical order), silently
        hiding the latest version behind an older one.
        """
        prefix = self._key_prefix(app_name, user_id, filename, session_id, segment) + "/"
        versions: set[int] = set()
        for key in list_files_in_s3(prefix=prefix):
            rest = key[len(prefix):]
            segment = rest.split("/", 1)[0]
            if segment.isdigit():
                versions.add(int(segment))
        return sorted(versions)

    def _get_artifact_version(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        version: Optional[int],
    ) -> Optional[ArtifactVersion]:
        if version is None:
            versions = self._list_versions(app_name, user_id, session_id, filename)
            if not versions:
                return None
            version = max(versions)

        key = self._key(app_name, user_id, filename, version, session_id)
        try:
            obj = get_object_with_metadata(key)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise

        create_time = (
            obj["last_modified"].timestamp() if obj.get("last_modified") else time.time()
        )
        return ArtifactVersion(
            version=version,
            canonical_uri=f"s3://{self._bucket_name}/{key}",
            create_time=create_time,
            mime_type=obj.get("content_type"),
            custom_metadata=obj.get("metadata") or {},
        )

    def _list_artifact_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> list[ArtifactVersion]:
        result: list[ArtifactVersion] = []
        for version in self._list_versions(app_name, user_id, session_id, filename):
            av = self._get_artifact_version(app_name, user_id, session_id, filename, version)
            if av is not None:
                result.append(av)
        return result


    # -- input artifacts (uploads) --------------------------------------
    #
    # Not part of ADK's artifact protocol (ADK only ever writes "output" --
    # what a tool produces). Uploads are a product concept -- see routers
    # /files.py -- layered on the exact same key scheme and S3 primitives
    # as the output side above, with "input" swapped in for "output" via
    # the `segment` parameter already threaded through _key/_key_prefix/
    # _list_versions. No ADK `types.Part` wrapping: uploads are raw bytes
    # with a content type, not ADK inline_data/text/file_data. No user_id/
    # "user:" namespace either -- input artifacts are always (app_name,
    # session_id) scoped, session_id being either a real session or the
    # literal "_shared" scope (see apowerb.artifacts.input_scope).

    async def save_input_artifact(
        self,
        *,
        app_name: str,
        session_id: str,
        filename: str,
        data: bytes,
        content_type: Optional[str] = None,
        custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        return await asyncio.to_thread(
            self._save_input_artifact,
            app_name,
            session_id,
            filename,
            data,
            content_type,
            custom_metadata,
        )

    async def load_input_artifact(
        self,
        *,
        app_name: str,
        session_id: str,
        filename: str,
        version: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(
            self._load_input_artifact, app_name, session_id, filename, version
        )

    async def list_input_artifact_filenames(
        self, *, app_name: str, session_id: str
    ) -> list[str]:
        return await asyncio.to_thread(
            self._list_input_artifact_filenames, app_name, session_id
        )

    def _save_input_artifact(
        self,
        app_name: str,
        session_id: str,
        filename: str,
        data: bytes,
        content_type: Optional[str],
        custom_metadata: Optional[dict[str, Any]],
    ) -> int:
        versions = self._list_versions(app_name, "", session_id, filename, segment=_INPUT_SEGMENT)
        version = 0 if not versions else max(versions) + 1
        metadata = (
            {k: str(v) for k, v in custom_metadata.items()} if custom_metadata else None
        )
        key = self._key(app_name, "", filename, version, session_id, segment=_INPUT_SEGMENT)
        upload_bytes_to_s3(
            data, key, content_type=content_type or "application/octet-stream", metadata=metadata
        )
        return version

    def _load_input_artifact(
        self,
        app_name: str,
        session_id: str,
        filename: str,
        version: Optional[int],
    ) -> Optional[dict[str, Any]]:
        if version is None:
            versions = self._list_versions(app_name, "", session_id, filename, segment=_INPUT_SEGMENT)
            if not versions:
                return None
            version = max(versions)

        key = self._key(app_name, "", filename, version, session_id, segment=_INPUT_SEGMENT)
        try:
            obj = get_object_with_metadata(key)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        if not obj["body"]:
            return None

        return {
            "data": obj["body"],
            "content_type": obj.get("content_type"),
            "version": version,
        }

    def _list_input_artifact_filenames(self, app_name: str, session_id: str) -> list[str]:
        prefix = f"{_ARTIFACTS_ROOT}/{app_name}/{session_id}/{_INPUT_SEGMENT}/"
        return sorted(self._filenames_under(prefix))


def register_s3_artifact_service() -> None:
    """Registers the "s3" URI scheme so ADK's service factory
    (``create_artifact_service_from_options``, called from
    ``get_fast_api_app``) resolves ``artifact_service_uri="s3://<bucket>"``
    into an ``S3ArtifactService``. The bucket name in the URI is informational
    only — the service itself reads ``settings.s3_bucket_name``, same as
    every other function in ``apowerb.storage.s3``.

    Idempotent: registering twice just overwrites the scheme with the same
    factory.
    """
    from google.adk.cli.service_registry import get_service_registry

    get_service_registry().register_artifact_service(
        "s3", lambda uri, **kwargs: S3ArtifactService()
    )
