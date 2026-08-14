"""Auto-migration for the 'user' table — adds missing columns for OAuth & Billing.
Also creates billing tables (transactions, credit_purchases) if they don't exist."""

from apowerb.configs.th2logger import setup_logging
from sqlalchemy import create_engine, text, inspect as sa_inspect

from apowerb.helpers.database_connection import DBConfig
from apowerb.configs.settings import get_settings

logger = setup_logging(__name__)


def ensure_user_columns():
    """Add new columns to the user table if they don't exist yet."""
    settings = get_settings()
    cfg = DBConfig()
    # Use same db_name as async URL, but with plain psycopg2 driver
    db_url = f"{cfg.db_type}://{cfg.db_user}:{cfg.db_password}@{cfg.db_host}:{cfg.db_port}/{cfg.db_name}"
    engine = create_engine(db_url)
    schema = settings.db_schema
    table_name = "user"

    try:
        inspector = sa_inspect(engine)
        if not inspector.has_table(table_name, schema=schema):
            print(
                f"[user_migration] Table '{schema}.{table_name}' does not exist, skipping."
            )
            return

        existing = [c["name"] for c in inspector.get_columns(table_name, schema=schema)]
        print(f"[user_migration] Existing columns: {existing}")

        new_cols = {
            # Core user fields
            "username": "VARCHAR(150)",
            "full_name": "VARCHAR(255)",
            "plan": "VARCHAR(50) DEFAULT 'free'",
            # OAuth — GitHub
            "avatar_url": "VARCHAR(500)",
            "github_id": "VARCHAR",
            "github_login": "VARCHAR",
            "github_access_token": "VARCHAR",
            # OAuth — Google
            "google_id": "VARCHAR",
            "google_access_token": "VARCHAR",
            # OAuth — Microsoft
            "microsoft_id": "VARCHAR",
            "microsoft_access_token": "VARCHAR",
            # OAuth — LinkedIn
            "linkedin_id": "VARCHAR",
            "linkedin_access_token": "VARCHAR",
            # Billing
            "credits": "NUMERIC(10,2) DEFAULT 0",
            "stripe_customer_id": "VARCHAR",
            # MFA
            "mfa_enabled": "BOOLEAN DEFAULT FALSE",
            "mfa_secret": "VARCHAR(255)",
            "mfa_backup_codes": "VARCHAR(1000)",
            # An administrator may demand a second factor from an account
            # that does not have one yet.
            "mfa_required": "BOOLEAN NOT NULL DEFAULT FALSE",
            # Revocation cut-off: tokens minted before it are refused. NULL
            # on every existing account, so nobody is signed out by the
            # migration itself.
            "sessions_valid_from": "TIMESTAMPTZ",
            # Onboarding
            "onboarding_completed": "BOOLEAN DEFAULT FALSE",
            # Email verification (B-notif): DEFAULT TRUE => backfill atomique des comptes existants
            "email_verified": "BOOLEAN NOT NULL DEFAULT TRUE",
        }

        schema_prefix = f'"{schema}".' if schema else ""
        added = []

        with engine.begin() as conn:
            for col, typ in new_cols.items():
                if col not in existing:
                    sql = f'ALTER TABLE {schema_prefix}"{table_name}" ADD COLUMN "{col}" {typ}'
                    print(f"[user_migration] Running: {sql}")
                    conn.execute(text(sql))
                    added.append(col)

        if added:
            print(f"[user_migration] Added columns to '{table_name}': {added}")
        else:
            print(f"[user_migration] Table '{table_name}' is up to date.")

        # --- Create billing tables if they don't exist ---
        _ensure_billing_tables(engine, inspector, schema)

    except Exception as e:
        print(f"[user_migration] ERROR: {e}")
        import traceback

        traceback.print_exc()
    finally:
        engine.dispose()


def _ensure_billing_tables(engine, inspector, schema):
    """Create transactions and credit_purchases tables if they don't exist."""
    schema_prefix = f'"{schema}".' if schema else ""

    with engine.begin() as conn:
        # --- transactions table ---
        if not inspector.has_table("transactions", schema=schema):
            print("[user_migration] Creating table 'transactions'...")
            conn.execute(
                text(f"""
                CREATE TABLE {schema_prefix}"transactions" (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES {schema_prefix}"user"(user_id),
                    type VARCHAR NOT NULL,
                    amount NUMERIC(10,2) NOT NULL,
                    balance_after NUMERIC(10,2) NOT NULL,
                    description VARCHAR,
                    metadata JSON,
                    stripe_payment_intent_id VARCHAR,
                    stripe_charge_id VARCHAR,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            )
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON {schema_prefix}"transactions"(user_id)'
                )
            )
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS idx_transactions_stripe_pi ON {schema_prefix}"transactions"(stripe_payment_intent_id)'
                )
            )
            print("[user_migration] Table 'transactions' created.")
        else:
            print("[user_migration] Table 'transactions' already exists.")

        # --- credit_purchases table ---
        if not inspector.has_table("credit_purchases", schema=schema):
            print("[user_migration] Creating table 'credit_purchases'...")
            conn.execute(
                text(f"""
                CREATE TABLE {schema_prefix}"credit_purchases" (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES {schema_prefix}"user"(user_id),
                    stripe_checkout_session_id VARCHAR UNIQUE,
                    stripe_payment_intent_id VARCHAR,
                    credits_amount NUMERIC(10,2) NOT NULL,
                    price_paid NUMERIC(10,2) NOT NULL,
                    currency VARCHAR DEFAULT 'usd',
                    status VARCHAR DEFAULT 'pending',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
            """)
            )
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS idx_cp_user_id ON {schema_prefix}"credit_purchases"(user_id)'
                )
            )
            conn.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS idx_cp_session ON {schema_prefix}"credit_purchases"(stripe_checkout_session_id)'
                )
            )
            print("[user_migration] Table 'credit_purchases' created.")
        else:
            print("[user_migration] Table 'credit_purchases' already exists.")
