import json
import pathlib
from logging import getLogger

from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset, models

logger = getLogger(__name__)

_PORTFOLIO_DIR = pathlib.Path(__file__).parent / "portfolio"


def _load_portfolio_skill(name: str) -> models.Skill | None:
    """Load a built-in skill from the portfolio directory."""
    skill_dir = _PORTFOLIO_DIR / name
    if not (skill_dir / "SKILL.md").exists():
        return None
    try:
        return load_skill_from_dir(skill_dir)
    except Exception as e:
        logger.warning("[SKILLS] Failed to load portfolio skill '%s': %s", name, e)
        return None


def _load_db_skill(name: str) -> models.Skill | None:
    """Load a custom skill from the database by name."""
    try:
        from apowerb.skills_store.skill_manager import skill_store

        t = skill_store.skill_table
        query = t.select().where(t.c.skill_name == name)
        rows = skill_store.get_list_skills(query)
        if not rows:
            return None

        row = rows[0]._asdict()
        instructions = row.get("instructions") or ""

        # Parse references
        references_raw = row.get("references_data")
        references = {}
        if references_raw:
            try:
                references = json.loads(references_raw) if isinstance(references_raw, str) else references_raw
            except (json.JSONDecodeError, TypeError):
                references = {}

        # Parse assets
        assets_raw = row.get("assets_data")
        assets = {}
        if assets_raw:
            try:
                assets = json.loads(assets_raw) if isinstance(assets_raw, str) else assets_raw
            except (json.JSONDecodeError, TypeError):
                assets = {}

        frontmatter = models.Frontmatter(
            name=row.get("skill_name", name),
            description=row.get("description", ""),
        )

        skill = models.Skill(
            frontmatter=frontmatter,
            instructions=instructions,
            resources=models.Resources(
                references=references,
                assets=assets,
            ),
        )
        return skill
    except Exception as e:
        logger.warning("[SKILLS] Failed to load DB skill '%s': %s", name, e)
        return None


def load_agent_skills(skill_names: list[str]) -> SkillToolset | None:
    """Load skills by name from portfolio or DB, return a SkillToolset or None."""
    skills = []
    for name in skill_names:
        skill = _load_portfolio_skill(name) or _load_db_skill(name)
        if skill:
            skills.append(skill)
        else:
            logger.warning("[SKILLS] Skill '%s' not found in portfolio or DB", name)
    return SkillToolset(skills=skills) if skills else None


def list_portfolio_skills() -> list[dict]:
    """List all built-in skills from the portfolio directory."""
    results = []
    if not _PORTFOLIO_DIR.exists():
        return results
    for skill_dir in sorted(_PORTFOLIO_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_md.exists():
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            # Parse simple frontmatter
            name = skill_dir.name
            description = ""
            instructions = content
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter_text = parts[1]
                    instructions = parts[2].strip()
                    for line in frontmatter_text.strip().splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("description:"):
                            description = line.split(":", 1)[1].strip().strip('"').strip("'")
            results.append({
                "skill_name": name,
                "description": description,
                "source": "portfolio",
                "instructions_preview": instructions[:200] if instructions else "",
            })
        except Exception as e:
            logger.warning("[SKILLS] Error reading portfolio skill '%s': %s", skill_dir.name, e)
    return results


def list_all_skills(organization_id: str = None, project_id: str = None) -> list[dict]:
    """List portfolio + custom skills."""
    results = list_portfolio_skills()

    # Add DB skills
    try:
        from apowerb.skills_store.skill_manager import skill_store

        t = skill_store.skill_table
        conditions = []
        if organization_id:
            conditions.append(t.c.organization_id == organization_id)
        if project_id:
            conditions.append(t.c.project_id == project_id)

        query = t.select()
        for cond in conditions:
            query = query.where(cond)

        rows = skill_store.get_list_skills(query)
        for row in rows:
            r = row._asdict()
            instructions = r.get("instructions") or ""
            refs_raw = r.get("references_data")
            refs = {}
            if refs_raw:
                try:
                    refs = json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
                except (json.JSONDecodeError, TypeError):
                    refs = {}
            results.append({
                "skill_id": r.get("skill_id"),
                "skill_name": r.get("skill_name"),
                "description": r.get("description", ""),
                "source": "custom",
                "owner_id": r.get("owner_id"),
                "is_public": r.get("is_public", "false"),
                "instructions": instructions,
                "instructions_preview": instructions[:200] if instructions else "",
                "references": refs,
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            })
    except Exception as e:
        logger.warning("[SKILLS] Error listing DB skills: %s", e)

    return results
