"""In-memory stand-in for the subset of the boto3 S3 client used by
``apowerb.storage.s3``.

No ``moto`` in this environment (checked: not installed), so this fake
implements just enough of the boto3 surface — ``upload_fileobj``,
``get_object``, ``head_object``, ``list_objects_v2``, ``delete_object`` — to
exercise the real ``apowerb.storage.s3`` functions and, through them, the
real ``S3ArtifactService`` logic, without a network call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import ClientError


class FakeS3Client:
    def __init__(self, page_size: int | None = None) -> None:
        self._objects: dict[str, dict[str, Any]] = {}
        # Real S3 caps a listing at 1000 keys per call; a small page size
        # exercises the continuation path without inventing 1000 objects.
        self._page_size = page_size
        self.list_calls = 0

    def upload_fileobj(self, fileobj, bucket: str, key: str, ExtraArgs=None) -> None:
        extra = ExtraArgs or {}
        self._objects[key] = {
            "body": fileobj.read(),
            "content_type": extra.get("ContentType", "application/octet-stream"),
            "metadata": dict(extra.get("Metadata") or {}),
            "last_modified": datetime.now(timezone.utc),
        }

    def upload_file(self, file_path: str, bucket: str, key: str) -> None:
        with open(file_path, "rb") as f:
            self._objects[key] = {
                "body": f.read(),
                "content_type": "application/octet-stream",
                "metadata": {},
                "last_modified": datetime.now(timezone.utc),
            }

    def get_object(self, Bucket: str, Key: str) -> dict:
        obj = self._objects.get(Key)
        if obj is None:
            raise _not_found(Key)
        return {
            "Body": _FakeBody(obj["body"]),
            "ContentType": obj["content_type"],
            "Metadata": obj["metadata"],
            "LastModified": obj["last_modified"],
        }

    def head_object(self, Bucket: str, Key: str) -> dict:
        obj = self._objects.get(Key)
        if obj is None:
            raise _not_found(Key)
        return {
            "ContentType": obj["content_type"],
            "Metadata": obj["metadata"],
            "LastModified": obj["last_modified"],
        }

    def list_objects_v2(self, Bucket: str, Prefix: str = "",
                        ContinuationToken: str | None = None) -> dict:
        self.list_calls += 1
        keys = sorted(k for k in self._objects if k.startswith(Prefix))

        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start:] if self._page_size is None else keys[start:start + self._page_size]
        if not page:
            return {}

        result: dict[str, Any] = {"Contents": [{"Key": k} for k in page]}
        nxt = start + len(page)
        if nxt < len(keys):
            result["IsTruncated"] = True
            result["NextContinuationToken"] = str(nxt)
        return result

    def delete_object(self, Bucket: str, Key: str) -> dict:
        self._objects.pop(Key, None)
        return {}


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _not_found(key: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": f"Key {key} not found"}},
        "GetObject",
    )
