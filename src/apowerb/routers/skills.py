import io
import json
import re
import zipfile
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from apowerb.auth.dependencies import get_current_user
from apowerb.users import schemas as user_schemas
from apowerb.helpers.emails import get_domain_from_email
from apowerb.helpers.safe_paths import contained_path
from apowerb.schema.skill_schema import SkillCreateSchema, SkillUpdateSchema
from apowerb.skills_store.skill_manager import skill_store
from apowerb.skills_store.skills_loader import (
    list_all_skills,
    list_portfolio_skills,
    _PORTFOLIO_DIR,
)

router = APIRouter()

# Portfolio skill directory names are our own kebab-case identifiers
# (dashboard-builder, rag-search, ...) — never arbitrary user input in
# practice, but skill_name still arrives as an unvalidated path parameter
# and was joined straight into _PORTFOLIO_DIR. A single ".." segment (a
# path parameter can't contain "/", so this is the only traversal a caller
# can reach) walked the export up one directory and, for the ZIP branch,
# would happily archive and return everything under it.
_PORTFOLIO_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@router.get("/skills", tags=["skills"])
async def list_skills(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List all skills (portfolio + user's custom skills)."""
    organization_id = get_domain_from_email(current_user.email)
    return list_all_skills(organization_id=organization_id)


@router.get("/skills/portfolio", tags=["skills"])
async def get_portfolio_skills(
    _current_user: user_schemas.User = Depends(get_current_user),
):
    """List built-in portfolio skills only."""
    return list_portfolio_skills()


@router.get("/skills/portfolio/{skill_name}/export", tags=["skills"])
async def export_portfolio_skill(
    skill_name: str,
    format: str = Query("json", pattern="^(json|adk)$"),
    _current_user: user_schemas.User = Depends(get_current_user),
):
    """Export a built-in portfolio skill as JSON or ADK ZIP."""
    if not _PORTFOLIO_SKILL_NAME_RE.match(skill_name):
        raise HTTPException(status_code=404, detail="Portfolio skill not found")
    skill_dir = Path(contained_path(_PORTFOLIO_DIR, skill_name))
    skill_md_path = Path(contained_path(skill_dir, "SKILL.md"))
    if not skill_dir.is_dir() or not skill_md_path.exists():
        raise HTTPException(status_code=404, detail="Portfolio skill not found")

    # Parse SKILL.md
    content = skill_md_path.read_text(encoding="utf-8")
    name = skill_name
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

    # Collect references
    references = {}
    refs_dir = Path(contained_path(skill_dir, "references"))
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.iterdir()):
            if ref_file.is_file():
                references[ref_file.name] = ref_file.read_text(encoding="utf-8")

    if format == "adk":
        # ZIP the portfolio directory as-is
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(skill_dir.rglob("*")):
                if file_path.is_file():
                    arcname = file_path.relative_to(skill_dir).as_posix()
                    zf.write(file_path, arcname)
        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}.zip"',
            },
        )

    # JSON format
    payload = {
        "skill_name": name,
        "description": description,
        "instructions": instructions,
        "references": references,
        "assets": {},
        "is_public": True,
    }
    json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        content=json_bytes,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.json"',
        },
    )


@router.post("/skills/import", tags=["skills"])
async def import_skill(
    file: UploadFile = File(...),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Import a skill from a JSON or ADK ZIP file."""
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("json", "zip"):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Please upload a .json or .zip file.",
        )

    file_bytes = await file.read()

    if ext == "json":
        try:
            data = json.loads(file_bytes)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid JSON file.")

        skill_name = data.get("skill_name")
        description = data.get("description", "")
        instructions = data.get("instructions", "")
        references = data.get("references")
        assets = data.get("assets")
        is_public = data.get("is_public", False)
        if not skill_name:
            raise HTTPException(
                status_code=400, detail="Missing 'skill_name' in JSON file."
            )

    else:
        # ZIP (ADK format)
        try:
            zf = zipfile.ZipFile(io.BytesIO(file_bytes))
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid ZIP file.")

        # Find SKILL.md (may be at root or inside a subdirectory)
        skill_md_entry = None
        for entry in zf.namelist():
            basename = entry.rsplit("/", 1)[-1] if "/" in entry else entry
            if basename == "SKILL.md":
                skill_md_entry = entry
                break
        if not skill_md_entry:
            zf.close()
            raise HTTPException(
                status_code=400,
                detail="ZIP file must contain a SKILL.md file.",
            )

        # Determine the base prefix (the directory containing SKILL.md)
        base_prefix = (
            skill_md_entry.rsplit("/", 1)[0] + "/"
            if "/" in skill_md_entry
            else ""
        )

        content = zf.read(skill_md_entry).decode("utf-8")
        skill_name = ""
        description = ""
        instructions = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_text = parts[1]
                instructions = parts[2].strip()
                for line in frontmatter_text.strip().splitlines():
                    if line.startswith("name:"):
                        skill_name = (
                            line.split(":", 1)[1].strip().strip('"').strip("'")
                        )
                    elif line.startswith("description:"):
                        description = (
                            line.split(":", 1)[1].strip().strip('"').strip("'")
                        )
        if not skill_name:
            skill_name = filename.rsplit(".", 1)[0]

        # Read references/
        references = {}
        refs_prefix = base_prefix + "references/"
        for entry in zf.namelist():
            if entry.startswith(refs_prefix) and not entry.endswith("/"):
                ref_name = entry[len(refs_prefix):]
                references[ref_name] = zf.read(entry).decode("utf-8")

        assets = {}
        is_public = False
        zf.close()

    # Create the skill in DB
    organization_id = get_domain_from_email(current_user.email)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    insert_query = (
        skill_store.skill_table.insert()
        .values(
            skill_name=skill_name,
            description=description,
            instructions=instructions,
            references_data=json.dumps(references) if references else None,
            assets_data=json.dumps(assets) if assets else None,
            owner_id=current_user.email,
            organization_id=organization_id,
            project_id="thaink2",
            is_public=str(is_public).lower(),
            created_at=now,
            updated_at=now,
            status="active",
        )
        .returning(skill_store.skill_table.c.skill_id)
    )

    try:
        with skill_store.engine.begin() as conn:
            skill_id = conn.execute(insert_query).scalar_one()
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A skill named '{skill_name}' already exists in this organization.",
        )

    return {
        "skill_id": skill_id,
        "skill_name": skill_name,
        "message": "Skill imported successfully.",
    }


@router.get("/skills/{skill_id}/export", tags=["skills"])
async def export_skill(
    skill_id: int,
    format: str = Query("json", pattern="^(json|adk)$"),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Export a custom skill as JSON or ADK ZIP."""
    organization_id = get_domain_from_email(current_user.email)
    t = skill_store.skill_table
    query = t.select().where(
        t.c.skill_id == skill_id,
        t.c.organization_id == organization_id,
    )
    rows = skill_store.get_list_skills(query)
    if not rows:
        raise HTTPException(status_code=404, detail="Skill not found")

    row = rows[0]._asdict()
    skill_name = row.get("skill_name", "skill")
    description = row.get("description", "")
    instructions = row.get("instructions", "")

    # Parse JSON fields
    references = {}
    refs_raw = row.get("references_data")
    if refs_raw:
        try:
            references = (
                json.loads(refs_raw) if isinstance(refs_raw, str) else refs_raw
            )
        except (json.JSONDecodeError, TypeError):
            pass

    assets = {}
    assets_raw = row.get("assets_data")
    if assets_raw:
        try:
            assets = (
                json.loads(assets_raw) if isinstance(assets_raw, str) else assets_raw
            )
        except (json.JSONDecodeError, TypeError):
            pass

    is_public = row.get("is_public", "false")

    if format == "adk":
        # Build ADK ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # SKILL.md with YAML frontmatter
            skill_md = f'---\nname: {skill_name}\ndescription: "{description}"\n---\n\n{instructions}'
            zf.writestr("SKILL.md", skill_md)

            # references/ directory
            if isinstance(references, dict):
                for ref_name, ref_content in references.items():
                    zf.writestr(f"references/{ref_name}", ref_content)

        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{skill_name}.zip"',
            },
        )

    # JSON format
    payload = {
        "skill_name": skill_name,
        "description": description,
        "instructions": instructions,
        "references": references,
        "assets": assets,
        "is_public": is_public == "true" if isinstance(is_public, str) else bool(is_public),
    }
    json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        content=json_bytes,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{skill_name}.json"',
        },
    )


@router.get("/skills/{skill_id}", tags=["skills"])
async def get_skill(
    skill_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Get a specific custom skill by ID."""
    organization_id = get_domain_from_email(current_user.email)
    t = skill_store.skill_table
    query = t.select().where(
        t.c.skill_id == skill_id,
        t.c.organization_id == organization_id,
    )
    rows = skill_store.get_list_skills(query)
    if not rows:
        raise HTTPException(status_code=404, detail="Skill not found")
    row = rows[0]._asdict()

    # Parse JSON fields
    for field in ("references_data", "assets_data"):
        raw = row.get(field)
        if raw and isinstance(raw, str):
            try:
                row[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass

    return row


@router.post("/skills", tags=["skills"])
async def create_skill(
    skill: SkillCreateSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Create a new custom skill."""
    organization_id = get_domain_from_email(current_user.email)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    insert_query = (
        skill_store.skill_table.insert()
        .values(
            skill_name=skill.skill_name,
            description=skill.description,
            instructions=skill.instructions,
            references_data=json.dumps(skill.references) if skill.references else None,
            assets_data=json.dumps(skill.assets) if skill.assets else None,
            owner_id=current_user.email,
            organization_id=organization_id,
            project_id=skill.project_id or "thaink2",
            is_public=str(skill.is_public).lower(),
            created_at=now,
            updated_at=now,
            status="active",
        )
        .returning(skill_store.skill_table.c.skill_id)
    )

    try:
        with skill_store.engine.begin() as conn:
            skill_id = conn.execute(insert_query).scalar_one()
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A skill named '{skill.skill_name}' already exists in this organization.",
        )

    return {
        "skill_id": skill_id,
        "skill_name": skill.skill_name,
        "message": "Skill created successfully.",
    }


@router.put("/skills/{skill_id}", tags=["skills"])
async def update_skill(
    skill_id: int,
    skill: SkillUpdateSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Update an existing custom skill."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t = skill_store.skill_table

    # Build update values from non-None fields
    values = {"updated_at": now}
    if skill.skill_name is not None:
        values["skill_name"] = skill.skill_name
    if skill.description is not None:
        values["description"] = skill.description
    if skill.instructions is not None:
        values["instructions"] = skill.instructions
    if skill.references is not None:
        values["references_data"] = json.dumps(skill.references)
    if skill.assets is not None:
        values["assets_data"] = json.dumps(skill.assets)
    if skill.is_public is not None:
        values["is_public"] = str(skill.is_public).lower()

    update_query = (
        t.update()
        .where(
            t.c.skill_id == skill_id,
            t.c.owner_id == current_user.email,
        )
        .values(**values)
    )

    with skill_store.engine.begin() as conn:
        result = conn.execute(update_query)
        if result.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="Skill not found or you do not have permission to update it.",
            )

    return {
        "skill_id": skill_id,
        "message": "Skill updated successfully.",
    }


@router.delete("/skills/{skill_id}", tags=["skills"])
async def delete_skill(
    skill_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Delete a custom skill."""
    t = skill_store.skill_table
    delete_query = t.delete().where(
        t.c.skill_id == skill_id,
        t.c.owner_id == current_user.email,
    )
    with skill_store.engine.begin() as conn:
        result = conn.execute(delete_query)
        if result.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="Skill not found or you do not have permission to delete it.",
            )

    return {"status": 200, "message": "Skill deleted successfully"}
