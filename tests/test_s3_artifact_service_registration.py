"""``register_s3_artifact_service`` wires the "s3" URI scheme into ADK's
service registry, so ``create_artifact_service_from_options`` (called from
``get_fast_api_app``) can resolve ``artifact_service_uri="s3://bucket"``
into an ``S3ArtifactService`` instance instead of raising "Unsupported
artifact service URI".
"""

from __future__ import annotations

from google.adk.cli.service_registry import get_service_registry

from apowerb.artifacts.s3_artifact_service import (
    S3ArtifactService,
    register_s3_artifact_service,
)


def test_registers_s3_scheme_resolving_to_s3_artifact_service():
    register_s3_artifact_service()
    service = get_service_registry().create_artifact_service(
        "s3://some-bucket", agents_dir="/tmp/agents"
    )
    assert isinstance(service, S3ArtifactService)


def test_registration_is_idempotent():
    register_s3_artifact_service()
    register_s3_artifact_service()
    service = get_service_registry().create_artifact_service(
        "s3://some-bucket", agents_dir="/tmp/agents"
    )
    assert isinstance(service, S3ArtifactService)
