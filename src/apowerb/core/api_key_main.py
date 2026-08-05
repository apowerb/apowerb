"""Saved API Keys — CRUD for user-scoped encrypted API keys."""

from apowerb.configs.th2logger import setup_logging
import re
from datetime import datetime

from apowerb.agent_store.api_key_store import ApiKeyStore
from apowerb.schema.api_key_schema import ApiKeyCreateSchema
from apowerb.helpers.encryptor import encrypt_value_in_dict, decrypt_value_in_dict

logger = setup_logging(__name__)

# DDL déplacé dans helpers/store_migrations.ensure_store_tables(),
# appelé au boot : importer ce module ne doit pas toucher la base.
api_key_store = ApiKeyStore()


def _parse_api_key_id(api_key_id: str) -> int:
    """Parse 'apikey{N}' format and return numeric ID."""
    match = re.fullmatch(r"apikey(\d+)", api_key_id)
    if not match:
        raise ValueError(f"Invalid API key ID format: {api_key_id}")
    return int(match.group(1))


def register_api_key(data: ApiKeyCreateSchema) -> dict:
    """Create a saved API key (encrypted)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = {
        "key_name": data.key_name,
        "provider": data.provider,
        "api_key_value": data.api_key_value,
        "model": data.model,
        "model_api_base": data.model_api_base,
        "owner_id": data.owner_id,
        "organization_id": data.organization_id,
        "created_at": now,
        "updated_at": now,
        "status": "active",
    }
    # Encrypt the API key value before storage
    values = encrypt_value_in_dict(values, ["api_key_value"])

    insert_q = (
        api_key_store.api_key_table.insert()
        .values(**values)
        .returning(api_key_store.api_key_table.c.api_key_id)
    )
    with api_key_store.engine.begin() as conn:
        key_id = conn.execute(insert_q).scalar_one()

    return {
        "api_key_id": f"apikey{key_id}",
        "key_name": data.key_name,
        "provider": data.provider,
        "model": data.model,
        "model_api_base": data.model_api_base,
        "message": "API key saved.",
    }


def list_user_api_keys(user_id: str) -> list[dict]:
    """List API keys for a user (decrypted)."""
    select_q = api_key_store.api_key_table.select().where(
        (api_key_store.api_key_table.c.owner_id == user_id)
        & (api_key_store.api_key_table.c.status == "active")
    )
    result = api_key_store.get_list(select_q)
    keys = []
    failed = 0
    for row in result:
        d = row._asdict()
        d["api_key_id"] = f"apikey{d['api_key_id']}"
        try:
            d = decrypt_value_in_dict(d, ["api_key_value"])
        except Exception:
            # Counted rather than named: there is no id parameter here to log
            # from, and the only other source is 0	~/Documents/Claude-workspace -- the row that holds
            # api_key_value. A failure count against the owner is what support
            # actually needs ("is it one key or all of them?"), and it cannot
            # grow into a secret leak.
            failed += 1
            d["api_key_value"] = ""
        keys.append(d)
    if failed:
        logger.warning(
            "Failed to decrypt %d of %d API keys for owner %s",
            failed, len(keys), user_id,
        )
    return keys


def get_api_key(api_key_id: str, user_id: str) -> dict | None:
    """Get a single API key by ID (decrypted)."""
    numeric_id = _parse_api_key_id(api_key_id)
    select_q = api_key_store.api_key_table.select().where(
        (api_key_store.api_key_table.c.api_key_id == numeric_id)
        & (api_key_store.api_key_table.c.owner_id == user_id)
    )
    result = api_key_store.get_list(select_q)
    rows = [r._asdict() for r in result]
    if rows:
        d = rows[0]
        d["api_key_id"] = f"apikey{d['api_key_id']}"
        try:
            d = decrypt_value_in_dict(d, ["api_key_value"])
        except Exception:
            # Logged from the caller's parameter, never from 0	~/Documents/Claude-workspace: that row
            # carries api_key_value, and reading any field back out of it to
            # build a log line puts the decrypted secret one careless edit
            # away from the log file.
            logger.warning("Failed to decrypt API key %s", api_key_id)
            d["api_key_value"] = ""
        return d
    return None


def delete_api_key(api_key_id: str, user_id: str) -> dict:
    """Delete an API key (ownership verified in SQL)."""
    numeric_id = _parse_api_key_id(api_key_id)
    # Single atomic DELETE with ownership check
    delete_q = api_key_store.api_key_table.delete().where(
        (api_key_store.api_key_table.c.api_key_id == numeric_id)
        & (api_key_store.api_key_table.c.owner_id == user_id)
    )
    with api_key_store.engine.begin() as conn:
        result = conn.execute(delete_q)
    if result.rowcount == 0:
        return {"error": "API key not found or not owned by you."}
    return {"message": "API key deleted."}
