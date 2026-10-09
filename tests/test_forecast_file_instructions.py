"""Le skill et le template Forecasting Agent doivent orienter l'agent vers
``file_id`` quand l'utilisateur a joint un fichier, sans import préalable ni
recopie de lignes."""
from pathlib import Path

from apowerb.core.superagents.templates.data import DATA_TEMPLATES

SKILL_FILE = (
    Path(__file__).resolve().parent.parent
    / "src" / "apowerb" / "skills_store" / "portfolio" / "forecasting" / "SKILL.md"
)


def _template():
    return next(t for t in DATA_TEMPLATES if t["template_id"] == "forecasting_agent")


class TestSkillAttachedFile:
    def test_names_the_param_and_the_ui_marker(self):
        content = SKILL_FILE.read_text()
        assert "file_id" in content
        assert "[Uploaded files" in content

    def test_attached_file_is_used_directly_without_import(self):
        content = SKILL_FILE.read_text().lower()
        assert "no need to import" in content

    def test_never_inline_rows_when_a_file_is_available(self):
        content = SKILL_FILE.read_text().lower()
        assert "never" in content and "rows" in content
        assert "inline" in content

    def test_columns_are_checked_with_read_uploaded_file_not_guessed(self):
        content = SKILL_FILE.read_text()
        assert "read_uploaded_file" in content


class TestTemplateAttachedFile:
    def test_instruction_routes_attachments_to_file_id(self):
        instruction = _template()["agent_instruction"]
        assert "file_id" in instruction
        assert "[Uploaded files" in instruction
        assert "never" in instruction.lower() and "rows" in instruction.lower()

    def test_readme_documents_xlsx_and_other_formats(self):
        readme = _template()["readme"].lower()
        assert "xlsx" in readme
