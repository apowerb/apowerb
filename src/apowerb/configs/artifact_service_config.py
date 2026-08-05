"""Picks the ADK artifact_service_uri: S3 when fully configured, the
existing file:// backend otherwise.

ADK ships only file://, gs:// and in-memory artifact backends — no S3. The
"s3" URI scheme is registered separately (see
``apowerb.artifacts.s3_artifact_service.register_s3_artifact_service``) so
ADK's service factory can resolve it. This module only decides which URI to
hand to ``get_fast_api_app``, so a deployment without S3 credentials still
boots on the local file backend instead of crashing at startup.
"""

from __future__ import annotations

import os

from apowerb.configs.settings import Settings


def is_s3_artifact_storage_configured(settings: Settings) -> bool:
    """True when S3 is selected and every credential needed to reach the
    bucket is present. Partial configuration (e.g. ``storage_mode=S3`` with
    a blank bucket name) is treated as not configured, not as an error."""
    if settings.storage_mode != "S3":
        return False
    return all([
        settings.s3_bucket_name,
        settings.s3_access_key,
        settings.s3_access_key_secret,
        settings.s3_endpoint,
        settings.s3_region,
    ])


def resolve_artifact_service_uri(settings: Settings, *, artifacts_dir: str) -> str:
    """Returns the artifact_service_uri for ADK's get_fast_api_app."""
    if is_s3_artifact_storage_configured(settings):
        return f"s3://{settings.s3_bucket_name}"
    return "file:///" + os.path.abspath(artifacts_dir).replace("\\", "/")
