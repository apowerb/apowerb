"""Bug report table, created at startup.

Same pattern as its neighbours (`notification_migration`, `webhook_migration`):
idempotent, called by `bootstrap()`, no-op when the table already exists.

Two schema choices worth noting:

- ``user_id`` is ``ON DELETE SET NULL``, not ``CASCADE``. A deleted account
  must not take its bug reports with it: the defect is still there.
  ``reporter_email`` is copied for the same reason — knowing *who* could
  reproduce it survives the account.
- ``duplicate_of`` rather than a simple counter: every report keeps its own
  logs and its own screenshot. A second witness to a defect often brings the
  information the first one lacked, and a counter alone would have thrown
  that away.
"""

from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, inspect, text

from apowerb.configs.settings import get_settings
from apowerb.helpers.database_connection import DBConfig

logger = setup_logging(__name__)
settings = get_settings()


def ensure_bug_reports_table() -> None:
    """Create the `bug_reports` table if it does not exist. Safe to repeat."""
    try:
        async_url = DBConfig().get_db_url()
        sync_url = async_url.replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            inspector = inspect(engine)
            existing = inspector.get_table_names(schema=settings.db_schema)

            if "bug_reports" not in existing:
                logger.info("Creating 'bug_reports' table...")
                conn.execute(
                    text(f"""
                    CREATE TABLE IF NOT EXISTS {settings.db_schema}.bug_reports (
                        id               SERIAL PRIMARY KEY,
                        user_id          INTEGER
                                             REFERENCES {settings.db_schema}."user"(user_id)
                                             ON DELETE SET NULL,
                        reporter_email   VARCHAR(320),
                        title            VARCHAR(200) NOT NULL,
                        where_i_was      TEXT,
                        what_i_did       TEXT,
                        expected         TEXT,
                        observed         TEXT,
                        area             VARCHAR(40)  NOT NULL DEFAULT 'other',
                        severity         VARCHAR(20)  NOT NULL DEFAULT 'major',
                        status           VARCHAR(30)  NOT NULL DEFAULT 'new',
                        fingerprint      VARCHAR(32)  NOT NULL,
                        occurrences      INTEGER      NOT NULL DEFAULT 1,
                        duplicate_of     INTEGER
                                             REFERENCES {settings.db_schema}.bug_reports(id)
                                             ON DELETE SET NULL,
                        route            VARCHAR(512),
                        run_id           VARCHAR(100),
                        server_version   VARCHAR(50),
                        context_json     TEXT,
                        api_calls_json   TEXT,
                        console_json     TEXT,
                        server_logs_json TEXT,
                        request_ids_json TEXT,
                        screenshot_path  VARCHAR(500),
                        issue_url        VARCHAR(500),
                        issue_number     INTEGER,
                        admin_note       TEXT,
                        created_at       TIMESTAMPTZ DEFAULT NOW(),
                        updated_at       TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                )
                # The triage screen reads "unprocessed, newest first" and
                # deduplication searches by fingerprint.
                # These are the only two hot queries.
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_bug_reports_status_created
                    ON {settings.db_schema}.bug_reports (status, created_at DESC);
                """)
                )
                conn.execute(
                    text(f"""
                    CREATE INDEX IF NOT EXISTS ix_bug_reports_fingerprint
                    ON {settings.db_schema}.bug_reports (fingerprint);
                """)
                )
                conn.commit()
                logger.info("'bug_reports' table created successfully.")
            else:
                # Column added after the first version of the table.
                # ADD COLUMN IF NOT EXISTS: free when it is already there,
                # and avoids a second migration function for a single field.
                conn.execute(
                    text(f"""
                    ALTER TABLE {settings.db_schema}.bug_reports
                    ADD COLUMN IF NOT EXISTS where_i_was TEXT,
                    ADD COLUMN IF NOT EXISTS area VARCHAR(40) NOT NULL DEFAULT 'other';
                """)
                )
                conn.commit()
                logger.debug("'bug_reports' table already exists -- columns ensured.")

            # The event log for each report. Created separately from `bug_reports`
            # and outside the "table absent" branch: a deployment that already has
            # the reports table must also receive the event log.
            # CASCADE: deleting a report takes its history with it, which has
            # no meaning without it.
            conn.execute(
                text(f"""
                CREATE TABLE IF NOT EXISTS {settings.db_schema}.bug_report_events (
                    id             SERIAL PRIMARY KEY,
                    bug_report_id  INTEGER NOT NULL
                                       REFERENCES {settings.db_schema}.bug_reports(id)
                                       ON DELETE CASCADE,
                    kind           VARCHAR(40) NOT NULL,
                    actor_user_id  INTEGER
                                       REFERENCES {settings.db_schema}."user"(user_id)
                                       ON DELETE SET NULL,
                    from_value     VARCHAR(60),
                    to_value       VARCHAR(60),
                    detail         TEXT,
                    created_at     TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            )
            # The timeline of a report, and the last alert sent: the two
            # reads on this log go through this pair of columns.
            conn.execute(
                text(f"""
                CREATE INDEX IF NOT EXISTS ix_bug_report_events_report_created
                ON {settings.db_schema}.bug_report_events (bug_report_id, created_at);
            """)
            )
            conn.commit()

        engine.dispose()
    except Exception as exc:
        # Like its neighbours: a migration that raises at boot would make the
        # server unusable for a secondary feature.
        logger.error("Failed to ensure 'bug_reports' table: %s", exc)


__all__ = ["ensure_bug_reports_table"]