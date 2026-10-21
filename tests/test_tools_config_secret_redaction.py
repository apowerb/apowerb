"""Les routes HTTP ne renvoient jamais les secrets d'une config d'outil
qu'on ne possède pas.

``list_user_tool_configs`` et ``fetch_tool_configs`` renvoient aussi les
configs ``system`` (partagées par les SuperAgents), déchiffrées : les
exécuteurs en ont besoin pour se connecter. Le 21/10/2026, ``GET
/api/tools_config`` a ainsi envoyé au navigateur d'un utilisateur quelconque le
mot de passe d'une connexion PostgreSQL système. Ces tests passent par les
routes réelles (TestClient) et cherchent le secret dans le corps brut de la
réponse, quel que soit le champ qui le porterait.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.pool import StaticPool

USER_A = "alice@example.com"
SYSTEM_VALUE = "xxxx-factice-systeme"
OWN_VALUE = "xxxx-factice-alice"


@pytest.fixture(autouse=True)
def _test_encrypt_key():
    from apowerb.helpers import encryptor as enc_mod

    original_fernet = enc_mod.fernet
    enc_mod.fernet = Fernet(Fernet.generate_key())
    try:
        yield
    finally:
        enc_mod.fernet = original_fernet


@pytest.fixture
def store():
    """Table ``tool_configs`` SQLite en mémoire derrière les helpers."""
    from apowerb.tools_store import tool_config as tc_mod

    # StaticPool : le TestClient sert la requête dans un autre thread, qui
    # doit voir la même base en mémoire.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata = MetaData()
    table = Table(
        "tool_configs",
        metadata,
        Column("tool_config_id", Integer, primary_key=True, autoincrement=True),
        Column("tool_config_name", String),
        Column("tool_name", String),
        Column("tool_config_params", String),
        Column("tool_category", String),
        Column("tool_config_type", String),
        Column("owner_id", String),
        Column("project_id", String),
        Column("organization_id", String),
        Column("created_at", String),
        Column("updated_at", String),
        Column("status", String),
    )
    metadata.create_all(engine)

    class _FakeStore:
        def __init__(self):
            self.engine = engine
            self.tool_config_table = table

        def get_list_tool_configs(self, query):
            with self.engine.connect() as conn:
                return conn.execute(query).fetchall()

    with patch.object(tc_mod.ToolConfigStore, "create_table", lambda self: None):
        from apowerb.tools_store import tools_helpers as th

        with patch.object(th, "tool_config_store", _FakeStore()):
            yield engine, table


def _insert(store, owner_id, name, params, tool_name="database.query", category="database"):
    from apowerb.helpers.encryptor import encrypt_value_in_dict

    engine, table = store
    encrypted = encrypt_value_in_dict(dict(params), values_to_encrypt=list(params))
    with engine.begin() as conn:
        result = conn.execute(
            table.insert().values(
                tool_config_name=name,
                tool_name=tool_name,
                tool_config_params=json.dumps(encrypted),
                tool_category=category,
                tool_config_type="active",
                owner_id=owner_id,
                project_id="p",
                organization_id="example.com",
                created_at="2026-10-21",
                updated_at="2026-10-21",
                status="active",
            )
        )
        return f"tool_config{result.inserted_primary_key[0]}"


@pytest.fixture
def client(store):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.data.upload_router import router as upload_router
    from apowerb.routers.tools import router as tools_router
    from apowerb.users import schemas as user_schemas

    app = FastAPI()
    app.include_router(tools_router)
    app.include_router(upload_router)
    app.dependency_overrides[get_current_user] = lambda: user_schemas.User.model_construct(
        email=USER_A
    )
    return TestClient(app)


def _system_db_params():
    return {"DB_HOST": "bi.example.com", "DB_USER": "svc", "DB_PASSWORD": SYSTEM_VALUE}


def test_list_masks_system_config_secrets(store, client):
    sys_id = _insert(store, "system", "Connexion BI (service)", _system_db_params())

    resp = client.get("/tools_config")

    assert resp.status_code == 200
    assert SYSTEM_VALUE not in resp.text
    cfg = next(c for c in resp.json() if c["tool_config_id"] == sys_id)
    # Les noms restent : l'UI s'en sert pour les conflits de clés.
    assert set(cfg["tool_config_params"]) == {"DB_HOST", "DB_USER", "DB_PASSWORD"}
    assert all(v is None for v in cfg["tool_config_params"].values())


def test_list_keeps_own_config_values_for_editing(store, client):
    own_id = _insert(store, USER_A, "ma-config", {"API_KEY": OWN_VALUE})

    cfg = next(c for c in client.get("/tools_config").json() if c["tool_config_id"] == own_id)

    # L'édition renvoie les valeurs pré-remplies en PUT : masquer écraserait le secret.
    assert cfg["tool_config_params"] == {"API_KEY": OWN_VALUE}


def test_fetch_by_id_masks_system_config_secrets(store, client):
    sys_id = _insert(store, "system", "Connexion BI (service)", _system_db_params())

    resp = client.get(f"/tools_config/{sys_id}")

    assert resp.status_code == 200
    assert SYSTEM_VALUE not in resp.text
    assert resp.json()["tool_config_params"]["DB_PASSWORD"] is None


def test_fetch_by_id_keeps_own_config_values(store, client):
    own_id = _insert(store, USER_A, "ma-config", {"API_KEY": OWN_VALUE})

    resp = client.get(f"/tools_config/{own_id}")

    assert resp.json()["tool_config_params"] == {"API_KEY": OWN_VALUE}


def test_mcp_configs_masks_system_headers_env_and_params(store, client):
    _insert(
        store,
        "system",
        "mcp-systeme",
        {
            "transport": "http",
            "name": "mcp-systeme",
            "url": "https://mcp.example.com",
            "header_Authorization": f"Bearer {SYSTEM_VALUE}",
            "param_token": SYSTEM_VALUE,
        },
        tool_name="mcp",
        category="mcp_server",
    )

    resp = client.get("/mcp_configs")

    assert resp.status_code == 200
    assert SYSTEM_VALUE not in resp.text
    server = resp.json()[0]
    assert server["url"] == "https://mcp.example.com"
    assert set(server["headers"]) == {"Authorization"}


def test_mcp_configs_keeps_own_headers(store, client):
    _insert(
        store,
        USER_A,
        "mon-mcp",
        {"transport": "http", "url": "https://x", "header_Authorization": OWN_VALUE},
        tool_name="mcp",
        category="mcp_server",
    )

    server = client.get("/mcp_configs").json()[0]

    assert server["headers"] == {"Authorization": OWN_VALUE}


def test_google_drive_configs_mask_system_tokens(store, client):
    _insert(
        store,
        "system",
        "drive-systeme",
        {"refresh_token": SYSTEM_VALUE},
        tool_name="google_drive",
        category="integration",
    )

    resp = client.get("/bi/tool-configs/google-drive")

    assert resp.status_code == 200
    assert SYSTEM_VALUE not in resp.text
    assert resp.json()[0]["tool_config_params"] == {"refresh_token": None}
