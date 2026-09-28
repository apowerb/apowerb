"""Tests pour le skill forecasting (lot A3)."""
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "src" / "apowerb" / "skills_store" / "portfolio" / "forecasting"
SKILL_FILE = SKILL_DIR / "SKILL.md"


class TestForecastingSkill:
    def test_skill_file_exists(self):
        assert SKILL_FILE.exists(), f"SKILL.md not found at {SKILL_FILE}"

    def test_skill_has_valid_frontmatter(self):
        content = SKILL_FILE.read_text()
        assert content.startswith("---")
        second_delim = content.index("---", 3)
        frontmatter = content[3:second_delim].strip()
        assert "name:" in frontmatter
        assert "description:" in frontmatter
        for line in frontmatter.split("\n"):
            if line.strip().startswith("name:"):
                assert line.split(":", 1)[1].strip() == "forecasting"

    def test_description_has_fr_and_en_keywords(self):
        content = SKILL_FILE.read_text().lower()
        for kw in ("prévision", "prédire", "anticiper", "tendance future", "mois prochains", "forecast", "predict"):
            assert kw in content, f"missing keyword {kw!r}"

    def test_mentions_required_tools(self):
        content = SKILL_FILE.read_text()
        for tool in (
            "tool_list_datasets", "tool_describe_dataset", "tool_describe_sql", "tool_text_to_sql",
            "tool_create_forecast_chart", "embed_chart",
            "tool_add_chart_to_dashboard",
        ):
            assert tool in content, f"missing tool {tool!r}"

    def test_mentions_history_length_guards(self):
        content = SKILL_FILE.read_text().lower()
        assert "8" in content
        assert "2" in content

    def test_loaded_by_list_portfolio_skills(self):
        from apowerb.skills_store.skills_loader import list_portfolio_skills

        names = [s["skill_name"] for s in list_portfolio_skills()]
        assert "forecasting" in names

    def test_loaded_by_load_agent_skills(self):
        from apowerb.skills_store.skills_loader import load_agent_skills

        toolset = load_agent_skills(["forecasting"])
        assert toolset is not None
        loaded_names = [s.frontmatter.name for s in toolset.skills]
        assert "forecasting" in loaded_names
