"""Tests for dashboard-builder skill file."""

import pytest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent / "src" / "apowerb" / "skills_store" / "portfolio" / "dashboard-builder"
SKILL_FILE = SKILL_DIR / "SKILL.md"


class TestDashboardBuilderSkill:
    def test_skill_file_exists(self):
        assert SKILL_FILE.exists(), f"SKILL.md not found at {SKILL_FILE}"

    def test_skill_has_valid_frontmatter(self):
        content = SKILL_FILE.read_text()
        assert content.startswith("---"), "SKILL.md must start with YAML frontmatter delimiter"
        # Find closing delimiter
        second_delim = content.index("---", 3)
        frontmatter = content[3:second_delim].strip()
        assert "name:" in frontmatter, "Frontmatter must contain 'name' field"
        assert "description:" in frontmatter, "Frontmatter must contain 'description' field"

        # Verify name value
        for line in frontmatter.split("\n"):
            if line.strip().startswith("name:"):
                name_value = line.split(":", 1)[1].strip()
                assert name_value == "dashboard-builder", f"Expected name 'dashboard-builder', got '{name_value}'"

    def test_skill_mentions_required_tools(self):
        content = SKILL_FILE.read_text()
        required_tools = [
            "tool_create_chart",
            "tool_create_dashboard",
            "tool_add_chart_to_dashboard",
            "tool_add_kpi_to_dashboard",
            "tool_publish_dashboard",
        ]
        for tool in required_tools:
            assert tool in content, f"SKILL.md must mention tool '{tool}'"

    def test_skill_has_keywords(self):
        content = SKILL_FILE.read_text().lower()
        keywords = ["dashboard", "chart", "kpi"]
        for kw in keywords:
            assert kw in content, f"SKILL.md must contain keyword '{kw}'"
