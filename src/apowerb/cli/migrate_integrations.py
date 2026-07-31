"""CLI entrypoint — re-encrypt legacy plaintext integration tokens.

Usage:

    python -m apowerb.cli.migrate_integrations --encrypt-legacy

The script scans the ``integrations`` table and, for every row whose
``access_token`` or ``refresh_token`` is stored in plaintext (i.e. cannot be
decrypted with the active Fernet key), re-encrypts it in place.

It is **idempotent** — already-encrypted rows are left untouched — and safe
to run multiple times.  Exits with code 0 on success, 2 on configuration
errors (missing ``ENCRYPT_KEY``).
"""

from __future__ import annotations

import argparse
import logging
import sys

from apowerb.configs.th2logger import setup_logging
from apowerb.integrations.helpers import encrypt_legacy_integration_tokens


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apowerb.cli.migrate_integrations",
        description=(
            "Re-encrypt plaintext OAuth tokens stored in the integrations table. "
            "Idempotent — safe to run repeatedly."
        ),
    )
    parser.add_argument(
        "--encrypt-legacy",
        action="store_true",
        help=(
            "Scan the integrations table and re-encrypt any row whose "
            "access_token/refresh_token is still plaintext."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logger = setup_logging(__name__)
    logging.getLogger().setLevel(logging.INFO)
    args = _build_parser().parse_args(argv)

    if not args.encrypt_legacy:
        logger.error(
            "No action requested. Pass --encrypt-legacy to run the migration."
        )
        return 2

    try:
        migrated = encrypt_legacy_integration_tokens()
    except RuntimeError as exc:
        logger.error("Migration aborted: %s", exc)
        return 2

    logger.info("Migration complete — %s row(s) re-encrypted.", migrated)
    print(f"Migrated {migrated} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
