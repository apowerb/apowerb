#!/usr/bin/env python3
"""Migrate BI CSV S3 keys from old format to new format.

Old format:  bi/{hashed_user_id}/{file_id}_{filename}.csv
New format:  bi/data/{organization_id}/{project_id}/data/{file_id}.csv

This script:
1. Queries all business_intelligence rows where type='data'
2. Detects rows whose config.s3_key still uses the old format
3. Copies the S3 object from old key -> new key
4. Updates the DB row's config with the new S3 key
5. Also updates any chart rows whose source.query references the old key

Usage:
    python scripts/migrate_s3_keys.py --dry-run   # preview changes
    python scripts/migrate_s3_keys.py              # execute migration

Idempotent: rows already using the new format are skipped.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
import sys

# Ensure the th2agent package is importable when running from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import create_engine, text

from th2agent.configs.settings import get_settings
from th2agent.helpers.database_connection import DBConfig

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("migrate_s3_keys")

# ---------------------------------------------------------------------------
# Settings & clients
# ---------------------------------------------------------------------------

settings = get_settings()

_NEW_PREFIX = "bi/data/"
_OLD_PREFIX = "bi/"
# Old keys look like:  bi/{hash16}/{uuid}_{name}.csv
# New keys look like:  bi/data/{org}/{proj}/data/{uuid}.csv
_OLD_KEY_PATTERN = re.compile(
    r"^bi/[0-9a-f]{16}/[0-9a-f\-]{36}_.+\.csv$"
)


def _is_old_format(key: str) -> bool:
    """Return True if *key* matches the legacy S3 key format."""
    return bool(_OLD_KEY_PATTERN.match(key))


def _is_new_format(key: str) -> bool:
    """Return True if *key* already uses the new layout."""
    return key.startswith(_NEW_PREFIX)


def _build_new_key(
    organization_id: str,
    project_id: str,
    file_id: str,
) -> str:
    """Replicate ``_bi_storage.build_file_key`` logic for CSV files."""
    org = _safe_segment(organization_id)
    proj = _safe_segment(project_id)
    return f"{_NEW_PREFIX}{org}/{proj}/data/{file_id}.csv"


def _safe_segment(value: str) -> str:
    """Mirror of ``_bi_storage._safe_segment``."""
    value = value.strip()
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"\s+", "-", value)
    return value or "default"


# ---------------------------------------------------------------------------
# S3 helpers (use the project's own boto3 config)
# ---------------------------------------------------------------------------


def _get_s3_client():
    import boto3

    return boto3.client(
        "s3",
        region_name=settings.s3_region,
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_access_key_secret,
    )


def _s3_key_exists(s3, bucket: str, key: str) -> bool:
    """Check whether an S3 object exists (HEAD request)."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def _s3_copy(s3, bucket: str, old_key: str, new_key: str) -> None:
    """Copy an S3 object within the same bucket."""
    s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": old_key},
        Key=new_key,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_sync_engine():
    """Create a synchronous SQLAlchemy engine (psycopg2)."""
    async_url = DBConfig().get_db_url()
    sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
    return create_engine(sync_url, echo=False)


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------


def migrate(dry_run: bool = True) -> None:
    engine = _get_sync_engine()
    schema = settings.db_schema
    bucket = settings.s3_bucket_name

    s3 = _get_s3_client()

    # ------------------------------------------------------------------
    # Phase 1 : migrate data rows (type = 'data')
    # ------------------------------------------------------------------
    logger.info("=== Phase 1: Migrate data row S3 keys ===")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT id, name, config, organization_id, project_id "
                f"FROM {schema}.business_intelligence "
                f"WHERE type = 'data' AND config IS NOT NULL"
            )
        ).fetchall()

    logger.info("Found %d data rows to inspect.", len(rows))

    migrated_keys: dict[str, str] = {}  # old_key -> new_key (for phase 2)
    stats = {"skipped": 0, "migrated": 0, "errors": 0, "already_new": 0}

    for row in rows:
        row_id = row[0]
        row_name = row[1]
        config = row[2] or {}
        org_id = row[3]
        proj_id = row[4]

        old_key = config.get("s3_key")

        # ---- guard: no key at all ----
        if not old_key:
            logger.debug("Row %s (%s): no s3_key in config, skipping.", row_id, row_name)
            stats["skipped"] += 1
            continue

        # ---- guard: already new format ----
        if _is_new_format(old_key):
            logger.debug("Row %s (%s): already new format, skipping.", row_id, row_name)
            stats["already_new"] += 1
            continue

        # ---- guard: unexpected format ----
        if not _is_old_format(old_key):
            logger.warning(
                "Row %s (%s): key '%s' does not match old or new pattern, skipping.",
                row_id, row_name, old_key,
            )
            stats["skipped"] += 1
            continue

        new_key = _build_new_key(org_id, proj_id, row_id)

        if dry_run:
            logger.info(
                "[DRY-RUN] Row %s (%s): would copy S3 '%s' -> '%s' and update DB.",
                row_id, row_name, old_key, new_key,
            )
            migrated_keys[old_key] = new_key
            stats["migrated"] += 1
            continue

        # ---- actual migration ----
        try:
            # Check if the new key already exists (idempotent re-run)
            if _s3_key_exists(s3, bucket, new_key):
                logger.info(
                    "Row %s (%s): new key '%s' already exists in S3, updating DB only.",
                    row_id, row_name, new_key,
                )
            else:
                # Verify old key exists before copying
                if not _s3_key_exists(s3, bucket, old_key):
                    logger.error(
                        "Row %s (%s): old key '%s' NOT FOUND in S3, skipping.",
                        row_id, row_name, old_key,
                    )
                    stats["errors"] += 1
                    continue

                _s3_copy(s3, bucket, old_key, new_key)
                logger.info(
                    "Row %s (%s): copied S3 '%s' -> '%s'.",
                    row_id, row_name, old_key, new_key,
                )

            # Update DB config
            new_config = dict(config)
            new_config["s3_key"] = new_key
            new_config["_migrated_from"] = old_key  # breadcrumb for traceability

            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE {schema}.business_intelligence "
                        f"SET config = CAST(:config AS JSONB), updated_at = NOW() "
                        f"WHERE id = :row_id"
                    ),
                    {"config": _json_dumps(new_config), "row_id": row_id},
                )

            migrated_keys[old_key] = new_key
            stats["migrated"] += 1

        except Exception as exc:
            logger.error(
                "Row %s (%s): migration failed: %s", row_id, row_name, exc, exc_info=True,
            )
            stats["errors"] += 1

    logger.info(
        "Phase 1 complete: %d migrated, %d already new, %d skipped, %d errors.",
        stats["migrated"], stats["already_new"], stats["skipped"], stats["errors"],
    )

    # ------------------------------------------------------------------
    # Phase 2 : update chart source queries that reference old CSV keys
    # ------------------------------------------------------------------
    if not migrated_keys:
        logger.info("=== Phase 2: No keys were migrated, nothing to patch in charts. ===")
        engine.dispose()
        return

    logger.info("=== Phase 2: Update chart configs referencing old CSV keys ===")

    with engine.connect() as conn:
        chart_rows = conn.execute(
            text(
                f"SELECT id, name, config "
                f"FROM {schema}.business_intelligence "
                f"WHERE type = 'chart' AND config IS NOT NULL"
            )
        ).fetchall()

    logger.info("Found %d chart rows to inspect.", len(chart_rows))
    chart_stats = {"updated": 0, "skipped": 0, "errors": 0}

    for crow in chart_rows:
        chart_id = crow[0]
        chart_name = crow[1]
        chart_config = crow[2] or {}

        # Charts store the CSV reference in source.query as "csv://<s3_key>"
        source = chart_config.get("source") or {}
        query = source.get("query", "")

        if not query.startswith("csv://"):
            chart_stats["skipped"] += 1
            continue

        current_key = query.removeprefix("csv://").strip()
        if current_key not in migrated_keys:
            chart_stats["skipped"] += 1
            continue

        new_key = migrated_keys[current_key]
        new_query = f"csv://{new_key}"

        if dry_run:
            logger.info(
                "[DRY-RUN] Chart %s (%s): would update query '%s' -> '%s'.",
                chart_id, chart_name, query, new_query,
            )
            chart_stats["updated"] += 1
            continue

        try:
            new_config = _deep_copy_config(chart_config)
            new_config["source"]["query"] = new_query

            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE {schema}.business_intelligence "
                        f"SET config = CAST(:config AS JSONB), updated_at = NOW() "
                        f"WHERE id = :chart_id"
                    ),
                    {"config": _json_dumps(new_config), "chart_id": chart_id},
                )

            logger.info(
                "Chart %s (%s): updated query to '%s'.", chart_id, chart_name, new_query,
            )
            chart_stats["updated"] += 1

        except Exception as exc:
            logger.error(
                "Chart %s (%s): failed to update: %s", chart_id, chart_name, exc, exc_info=True,
            )
            chart_stats["errors"] += 1

    logger.info(
        "Phase 2 complete: %d charts updated, %d skipped, %d errors.",
        chart_stats["updated"], chart_stats["skipped"], chart_stats["errors"],
    )

    engine.dispose()
    logger.info("Migration finished.")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _json_dumps(obj: dict) -> str:
    """Serialize a dict to a JSON string for PostgreSQL JSONB columns."""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _deep_copy_config(config: dict) -> dict:
    """Return a deep copy so we don't mutate the original."""
    return copy.deepcopy(config)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate BI CSV S3 keys from old to new format.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview what would change without making any modifications.",
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("Running in DRY-RUN mode. No changes will be made.")
    else:
        logger.info("Running in LIVE mode. Changes will be applied.")

    migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
