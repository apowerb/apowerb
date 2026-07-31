"""Comprehensive tests for the skills module: schema, router, and loader."""

import json
import pathlib
import textwrap
from collections import namedtuple
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apowerb.schema.skill_schema import SkillCreateSchema, SkillUpdateSchema
from apowerb.users import schemas as user_schemas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_user(**overrides) -> user_schemas.User:
    """Build a fake User schema instance for dependency injection."""
    defaults = dict(
        user_id=1,
        email="test@example.com",
        role="USER",
        first_name="Test",
        last_name="User",
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )
    defaults.update(overrides)
    return user_schemas.User(**defaults)


# A lightweight named-tuple that mimics SQLAlchemy Row._asdict()
SkillRow = namedtuple(
    "SkillRow",
    [
        "skill_id",
        "skill_name",
        "description",
        "instructions",
        "references_data",
        "assets_data",
        "owner_id",
        "organization_id",
        "project_id",
        "is_public",
        "created_at",
        "updated_at",
        "status",
    ],
)


def _make_row(**overrides) -> SkillRow:
    defaults = dict(
        skill_id=1,
        skill_name="my-skill",
        description="A test skill",
        instructions="Do things.",
        references_data=None,
        assets_data=None,
        owner_id="test@example.com",
        organization_id="example.com",
        project_id="thaink2",
        is_public="false",
        created_at="2025-01-01 00:00:00",
        updated_at="2025-01-01 00:00:00",
        status="active",
    )
    defaults.update(overrides)
    return SkillRow(**defaults)


# ═══════════════════════════════════════════════════════════════════════════
# A. SCHEMA VALIDATION TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestSkillCreateSchemaName:
    """Validate skill_name kebab-case and length constraints."""

    @pytest.mark.parametrize(
        "name",
        [
            "my-skill",
            "text-to-sql",
            "a",
            "skill-123",
            "abc",
            "a-b-c",
            "x1-y2-z3",
            "0",
            "123",
        ],
    )
    def test_valid_kebab_names(self, name: str):
        schema = SkillCreateSchema(skill_name=name, description="ok")
        assert schema.skill_name == name

    @pytest.mark.parametrize(
        "name, reason",
        [
            ("My Skill", "spaces and uppercase"),
            ("UPPERCASE", "all uppercase"),
            ("with_underscore", "underscore"),
            ("with space", "space"),
            ("-leading-dash", "leading dash"),
            ("trailing-dash-", "trailing dash"),
            ("special!char", "special character"),
            ("", "empty string"),
            ("Hello", "capitalised"),
            ("a--b", "double dash"),
            ("ALLCAPS", "all caps"),
        ],
    )
    def test_invalid_kebab_names(self, name: str, reason: str):
        with pytest.raises(ValidationError) as exc_info:
            SkillCreateSchema(skill_name=name, description="ok")
        assert "skill_name" in str(exc_info.value)

    def test_name_at_max_length(self):
        name = "a" * 64
        schema = SkillCreateSchema(skill_name=name, description="ok")
        assert schema.skill_name == name

    def test_name_too_long(self):
        name = "a" * 65
        with pytest.raises(ValidationError) as exc_info:
            SkillCreateSchema(skill_name=name, description="ok")
        assert "skill_name" in str(exc_info.value)

    def test_name_just_over_max(self):
        # 65 chars of valid kebab
        name = "a-" * 32 + "b"  # 65 chars
        with pytest.raises(ValidationError):
            SkillCreateSchema(skill_name=name, description="ok")


class TestSkillCreateSchemaDescription:
    """Validate description length constraints."""

    def test_valid_description(self):
        schema = SkillCreateSchema(skill_name="ok", description="A good description")
        assert schema.description == "A good description"

    def test_description_at_max_length(self):
        desc = "x" * 1024
        schema = SkillCreateSchema(skill_name="ok", description=desc)
        assert len(schema.description) == 1024

    def test_description_too_long(self):
        desc = "x" * 1025
        with pytest.raises(ValidationError) as exc_info:
            SkillCreateSchema(skill_name="ok", description=desc)
        assert "description" in str(exc_info.value)

    def test_empty_description_allowed(self):
        schema = SkillCreateSchema(skill_name="ok", description="")
        assert schema.description == ""


class TestSkillCreateSchemaDefaults:
    """Verify default values on SkillCreateSchema."""

    def test_defaults(self):
        schema = SkillCreateSchema(skill_name="ok", description="desc")
        assert schema.instructions == ""
        assert schema.references is None
        assert schema.assets is None
        assert schema.is_public is False
        assert schema.project_id == "thaink2"

    def test_full_create_payload(self):
        schema = SkillCreateSchema(
            skill_name="my-skill",
            description="desc",
            instructions="Step 1",
            references={"url": "https://example.com"},
            assets={"file": "data.csv"},
            is_public=True,
            project_id="custom",
        )
        assert schema.skill_name == "my-skill"
        assert schema.instructions == "Step 1"
        assert schema.references == {"url": "https://example.com"}
        assert schema.is_public is True
        assert schema.project_id == "custom"


class TestSkillUpdateSchema:
    """SkillUpdateSchema allows None values to pass through but validates non-None."""

    def test_all_none_defaults(self):
        schema = SkillUpdateSchema()
        assert schema.skill_name is None
        assert schema.description is None
        assert schema.instructions is None
        assert schema.references is None
        assert schema.assets is None
        assert schema.is_public is None

    def test_none_skill_name_passes(self):
        schema = SkillUpdateSchema(skill_name=None)
        assert schema.skill_name is None

    def test_none_description_passes(self):
        schema = SkillUpdateSchema(description=None)
        assert schema.description is None

    def test_valid_skill_name_passes(self):
        schema = SkillUpdateSchema(skill_name="new-name")
        assert schema.skill_name == "new-name"

    def test_invalid_skill_name_rejected(self):
        with pytest.raises(ValidationError):
            SkillUpdateSchema(skill_name="Invalid Name")

    def test_valid_description_passes(self):
        schema = SkillUpdateSchema(description="Updated description")
        assert schema.description == "Updated description"

    def test_description_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SkillUpdateSchema(description="x" * 1025)

    def test_name_too_long_rejected(self):
        with pytest.raises(ValidationError):
            SkillUpdateSchema(skill_name="a" * 65)

    def test_partial_update(self):
        schema = SkillUpdateSchema(skill_name="new-name", is_public=True)
        assert schema.skill_name == "new-name"
        assert schema.is_public is True
        assert schema.description is None
        assert schema.instructions is None


# ═══════════════════════════════════════════════════════════════════════════
# B. ROUTER ENDPOINT TESTS
# ═══════════════════════════════════════════════════════════════════════════

# We patch the skill_store at the module where it is used by the router
# before importing the router, so we need to mock it at import time.


@pytest.fixture()
def mock_skill_store():
    """Create a MagicMock standing in for SkillStore with a fake skill_table."""
    store = MagicMock()
    # Build a minimal mock table with .c attribute for column references,
    # plus .select(), .insert(), .update(), .delete() that return mock queries.
    table = MagicMock()
    store.skill_table = table
    store.engine = MagicMock()
    return store


@pytest.fixture()
def client(mock_skill_store):
    """
    Build a minimal FastAPI TestClient that uses the skills router
    with auth and skill_store mocked out.
    """
    # Patch skill_store before importing the router module
    with patch("apowerb.skills_store.skill_manager.skill_store", mock_skill_store), \
         patch("apowerb.skills_store.skill_manager.SkillStore", return_value=mock_skill_store):
        # Re-patch at the router module level as well, since it imports at top level
        with patch("apowerb.routers.skills.skill_store", mock_skill_store), \
             patch("apowerb.routers.skills.list_all_skills") as mock_list_all, \
             patch("apowerb.routers.skills.list_portfolio_skills") as mock_list_portfolio:

            from apowerb.routers.skills import router

            app = FastAPI()
            app.include_router(router, prefix="/api")

            fake_user = _fake_user()

            async def override_get_current_user():
                return fake_user

            from apowerb.auth.dependencies import get_current_user
            app.dependency_overrides[get_current_user] = override_get_current_user

            test_client = TestClient(app)

            # Attach mocks so tests can configure them
            test_client._mock_store = mock_skill_store
            test_client._mock_list_all = mock_list_all
            test_client._mock_list_portfolio = mock_list_portfolio
            test_client._fake_user = fake_user

            yield test_client


class TestListSkills:
    """GET /api/skills - list all skills."""

    def test_list_skills_returns_combined(self, client):
        client._mock_list_all.return_value = [
            {"skill_name": "text-to-sql", "source": "portfolio"},
            {"skill_name": "my-custom", "source": "custom"},
        ]
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        # Verify it was called with organization_id derived from email
        client._mock_list_all.assert_called_once_with(organization_id="example.com")

    def test_list_skills_empty(self, client):
        client._mock_list_all.return_value = []
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        assert resp.json() == []


class TestListPortfolioSkills:
    """GET /api/skills/portfolio - portfolio only."""

    def test_list_portfolio_skills(self, client):
        client._mock_list_portfolio.return_value = [
            {"skill_name": "text-to-sql", "source": "portfolio"},
        ]
        resp = client.get("/api/skills/portfolio")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestGetSkillById:
    """GET /api/skills/{skill_id}."""

    def test_get_skill_found(self, client):
        row = _make_row(organization_id="example.com")
        client._mock_store.get_list_skills.return_value = [row]

        resp = client.get("/api/skills/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_name"] == "my-skill"
        assert data["organization_id"] == "example.com"

    def test_get_skill_not_found(self, client):
        client._mock_store.get_list_skills.return_value = []

        resp = client.get("/api/skills/999")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_get_skill_wrong_organization(self, client):
        # The router filters by organization_id = "example.com" (from test@example.com).
        # If no rows match that filter, it returns 404.
        client._mock_store.get_list_skills.return_value = []

        resp = client.get("/api/skills/1")
        assert resp.status_code == 404

    def test_get_skill_parses_json_references(self, client):
        refs = json.dumps({"url": "https://example.com"})
        row = _make_row(references_data=refs)
        client._mock_store.get_list_skills.return_value = [row]

        resp = client.get("/api/skills/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["references_data"] == {"url": "https://example.com"}

    def test_get_skill_parses_json_assets(self, client):
        assets = json.dumps({"file": "data.csv"})
        row = _make_row(assets_data=assets)
        client._mock_store.get_list_skills.return_value = [row]

        resp = client.get("/api/skills/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["assets_data"] == {"file": "data.csv"}

    def test_get_skill_handles_invalid_json_gracefully(self, client):
        row = _make_row(references_data="not-json{{{")
        client._mock_store.get_list_skills.return_value = [row]

        resp = client.get("/api/skills/1")
        assert resp.status_code == 200
        # Invalid JSON stays as-is (string)
        data = resp.json()
        assert data["references_data"] == "not-json{{{"


class TestCreateSkill:
    """POST /api/skills."""

    def test_create_valid_skill(self, client):
        # Mock engine.begin() context manager to return a mock connection
        mock_conn = MagicMock()
        mock_conn.execute.return_value.scalar_one.return_value = 42
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.post(
            "/api/skills",
            json={
                "skill_name": "my-new-skill",
                "description": "A new skill",
                "instructions": "Follow these steps",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_id"] == 42
        assert data["skill_name"] == "my-new-skill"
        assert "created" in data["message"].lower()

    def test_create_invalid_skill_name_422(self, client):
        resp = client.post(
            "/api/skills",
            json={
                "skill_name": "Invalid Name!",
                "description": "ok",
            },
        )
        assert resp.status_code == 422

    def test_create_skill_name_too_long_422(self, client):
        resp = client.post(
            "/api/skills",
            json={
                "skill_name": "a" * 65,
                "description": "ok",
            },
        )
        assert resp.status_code == 422

    def test_create_description_too_long_422(self, client):
        resp = client.post(
            "/api/skills",
            json={
                "skill_name": "ok",
                "description": "x" * 1025,
            },
        )
        assert resp.status_code == 422

    def test_create_missing_required_fields_422(self, client):
        resp = client.post("/api/skills", json={})
        assert resp.status_code == 422

    def test_create_missing_description_422(self, client):
        resp = client.post("/api/skills", json={"skill_name": "ok"})
        assert resp.status_code == 422

    def test_create_skill_conflict_409(self, client):
        from sqlalchemy.exc import IntegrityError

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = IntegrityError(
            "duplicate", params=None, orig=Exception("unique violation")
        )
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.post(
            "/api/skills",
            json={
                "skill_name": "duplicate-skill",
                "description": "Already exists",
            },
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"].lower()


class TestUpdateSkill:
    """PUT /api/skills/{skill_id}."""

    def test_update_skill_success(self, client):
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_conn.execute.return_value = mock_result
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.put(
            "/api/skills/1",
            json={"skill_name": "updated-name"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_id"] == 1
        assert "updated" in data["message"].lower()

    def test_update_skill_not_owner_404(self, client):
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0  # No rows matched = not owner or not found
        mock_conn.execute.return_value = mock_result
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.put(
            "/api/skills/999",
            json={"description": "attempt"},
        )
        assert resp.status_code == 404
        assert "permission" in resp.json()["detail"].lower()

    def test_update_invalid_name_422(self, client):
        resp = client.put(
            "/api/skills/1",
            json={"skill_name": "Bad Name"},
        )
        assert resp.status_code == 422

    def test_update_description_too_long_422(self, client):
        resp = client.put(
            "/api/skills/1",
            json={"description": "x" * 1025},
        )
        assert resp.status_code == 422

    def test_update_with_all_none_succeeds(self, client):
        """Sending an empty update body (all None) should still succeed if the row exists."""
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_conn.execute.return_value = mock_result
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.put("/api/skills/1", json={})
        assert resp.status_code == 200


class TestDeleteSkill:
    """DELETE /api/skills/{skill_id}."""

    def test_delete_skill_success(self, client):
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_conn.execute.return_value = mock_result
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.delete("/api/skills/1")
        assert resp.status_code == 200
        assert "deleted" in resp.json()["message"].lower()

    def test_delete_skill_not_owner_404(self, client):
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_conn.execute.return_value = mock_result
        client._mock_store.engine.begin.return_value.__enter__ = MagicMock(return_value=mock_conn)
        client._mock_store.engine.begin.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.delete("/api/skills/999")
        assert resp.status_code == 404
        assert "permission" in resp.json()["detail"].lower()


class TestAuthRequired:
    """Verify that endpoints require authentication when no override is set."""

    def test_no_auth_returns_401_or_403(self):
        """Without the dependency override, requests should fail authentication."""
        with patch("apowerb.routers.skills.skill_store", MagicMock()), \
             patch("apowerb.routers.skills.list_all_skills"), \
             patch("apowerb.routers.skills.list_portfolio_skills"):
            from apowerb.routers.skills import router

            app = FastAPI()
            app.include_router(router, prefix="/api")
            # No dependency override => real get_current_user will fail
            no_auth_client = TestClient(app)

            resp = no_auth_client.get("/api/skills")
            # Should be 401 (not authenticated) or 403
            assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
# C. SKILLS LOADER TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestListPortfolioSkillsLoader:
    """Test list_portfolio_skills from the loader module."""

    def test_reads_skills_from_portfolio_dir(self, tmp_path):
        """Create a temporary portfolio directory with SKILL.md files."""
        skill_dir = tmp_path / "my-test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: my-test-skill
            description: "A test skill for unit testing"
            ---

            # Instructions
            Do the thing.
        """))

        # Also create a directory without SKILL.md to verify it's skipped
        no_skill_dir = tmp_path / "no-skill-here"
        no_skill_dir.mkdir()

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path):
            from apowerb.skills_store.skills_loader import list_portfolio_skills
            result = list_portfolio_skills()

        assert len(result) == 1
        assert result[0]["skill_name"] == "my-test-skill"
        assert result[0]["description"] == "A test skill for unit testing"
        assert result[0]["source"] == "portfolio"
        assert "Instructions" in result[0]["instructions_preview"]

    def test_no_portfolio_dir_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "does-not-exist"
        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", nonexistent):
            from apowerb.skills_store.skills_loader import list_portfolio_skills
            result = list_portfolio_skills()
        assert result == []

    def test_portfolio_without_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "simple-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("Just plain instructions here.\n")

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path):
            from apowerb.skills_store.skills_loader import list_portfolio_skills
            result = list_portfolio_skills()

        assert len(result) == 1
        # Name falls back to directory name
        assert result[0]["skill_name"] == "simple-skill"
        assert result[0]["description"] == ""
        assert "plain instructions" in result[0]["instructions_preview"]

    def test_multiple_portfolio_skills_sorted(self, tmp_path):
        for name in ["zzz-last", "aaa-first", "mmm-middle"]:
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: desc\n---\nInstructions")

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path):
            from apowerb.skills_store.skills_loader import list_portfolio_skills
            result = list_portfolio_skills()

        assert len(result) == 3
        names = [r["skill_name"] for r in result]
        assert names == ["aaa-first", "mmm-middle", "zzz-last"]


class TestLoadDbSkill:
    """Test _load_db_skill from the loader module."""

    def test_load_db_skill_found(self):
        row = _make_row(
            skill_name="db-skill",
            description="From DB",
            instructions="Do DB things.",
            references_data=json.dumps({"ref": "https://example.com"}),
            assets_data=json.dumps({"file": "data.csv"}),
        )
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = [row]

        with patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import _load_db_skill
            result = _load_db_skill("db-skill")

        assert result is not None
        assert result.frontmatter.name == "db-skill"
        assert result.frontmatter.description == "From DB"
        assert result.instructions == "Do DB things."
        assert result.resources.references == {"ref": "https://example.com"}
        assert result.resources.assets == {"file": "data.csv"}

    def test_load_db_skill_not_found(self):
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = []

        with patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import _load_db_skill
            result = _load_db_skill("nonexistent")

        assert result is None

    def test_load_db_skill_with_invalid_json_references(self):
        row = _make_row(
            references_data="bad-json{{{",
            assets_data="also-bad",
        )
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = [row]

        with patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import _load_db_skill
            result = _load_db_skill("my-skill")

        assert result is not None
        # Invalid JSON should default to empty dict
        assert result.resources.references == {}
        assert result.resources.assets == {}

    def test_load_db_skill_with_none_references(self):
        row = _make_row(references_data=None, assets_data=None)
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = [row]

        with patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import _load_db_skill
            result = _load_db_skill("my-skill")

        assert result is not None
        assert result.resources.references == {}
        assert result.resources.assets == {}

    def test_load_db_skill_with_none_instructions(self):
        row = _make_row(instructions=None)
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = [row]

        with patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import _load_db_skill
            result = _load_db_skill("my-skill")

        assert result is not None
        assert result.instructions == ""


class TestLoadAgentSkills:
    """Test load_agent_skills from the loader module."""

    def test_load_portfolio_skill_by_name(self, tmp_path):
        skill_dir = tmp_path / "my-portfolio-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-portfolio-skill\ndescription: desc\n---\nInstructions"
        )

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path), \
             patch("apowerb.skills_store.skills_loader.load_skill_from_dir") as mock_load:
            mock_skill = MagicMock()
            mock_load.return_value = mock_skill

            from apowerb.skills_store.skills_loader import load_agent_skills
            result = load_agent_skills(["my-portfolio-skill"])

        assert result is not None

    def test_load_nonexistent_skill_returns_none(self, tmp_path):
        """When no skills are found, load_agent_skills returns None."""
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = []

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path), \
             patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import load_agent_skills
            result = load_agent_skills(["nonexistent-skill"])

        assert result is None

    def test_load_empty_list_returns_none(self):
        from apowerb.skills_store.skills_loader import load_agent_skills
        result = load_agent_skills([])
        assert result is None


class TestListAllSkills:
    """Test list_all_skills combining portfolio and DB."""

    def test_combines_portfolio_and_db(self, tmp_path):
        # Set up a portfolio skill
        skill_dir = tmp_path / "portfolio-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: portfolio-skill\ndescription: portfolio desc\n---\nInstructions"
        )

        db_row = _make_row(
            skill_id=10,
            skill_name="db-skill",
            description="db desc",
            owner_id="user@org.com",
            organization_id="org.com",
        )
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = [db_row]

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path), \
             patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import list_all_skills
            result = list_all_skills(organization_id="org.com")

        # Should have 1 portfolio + 1 db skill
        assert len(result) == 2
        sources = {r["source"] for r in result}
        assert sources == {"portfolio", "custom"}

    def test_filters_by_organization(self, tmp_path):
        mock_store = MagicMock()
        mock_store.get_list_skills.return_value = []

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path), \
             patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import list_all_skills
            list_all_skills(organization_id="specific-org.com")

        # Verify that the query was constructed (the mock was called)
        mock_store.get_list_skills.assert_called_once()

    def test_handles_db_error_gracefully(self, tmp_path):
        mock_store = MagicMock()
        mock_store.get_list_skills.side_effect = Exception("DB connection failed")

        with patch("apowerb.skills_store.skills_loader._PORTFOLIO_DIR", tmp_path), \
             patch("apowerb.skills_store.skill_manager.skill_store", mock_store):
            from apowerb.skills_store.skills_loader import list_all_skills
            result = list_all_skills(organization_id="org.com")

        # Should still return portfolio skills (empty in this case) without crashing
        assert isinstance(result, list)
