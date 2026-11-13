"""Filtre des tables système (Keycloak/Liquibase) dans l'introspection text_to_sql.

Régression : des préfixes trop larges (« client », « component », « credential »…)
masquaient des tables métier comme ``clients`` ; l'agent ne voyait plus la table
et ses requêtes SQL échouaient.
"""

from __future__ import annotations

import pytest

from apowerb.tools_store.portfolio.text_to_sql import _is_excluded_table


@pytest.mark.parametrize(
    "table",
    [
        "clients",
        "client_orders",
        "components",
        "credentials",
        "groups_members",
        "realms_map",
        "federation_events",
        "offline_orders",
        "colis",
        "conversations",
    ],
)
def test_business_tables_are_kept(table):
    assert not _is_excluded_table(table)


@pytest.mark.parametrize(
    "table",
    [
        "client",
        "client_scope",
        "component_config",
        "credential",
        "group_role_mapping",
        "realm",
        "realm_attribute",
        "fed_user_attribute",
        "federated_identity",
        "offline_user_session",
        "authentication_flow",
        "user_entity",
        "keycloak_role",
        "databasechangelog",
        "databasechangeloglock",
        "pwd_mngt",
    ],
)
def test_keycloak_tables_are_excluded(table):
    assert _is_excluded_table(table)

