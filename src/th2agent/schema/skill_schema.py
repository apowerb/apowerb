import re

from pydantic import BaseModel, field_validator

_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MAX_NAME_LEN = 64
_MAX_DESC_LEN = 1024


def _validate_skill_name(v: str) -> str:
    if len(v) > _MAX_NAME_LEN:
        raise ValueError(f"skill_name must be at most {_MAX_NAME_LEN} characters")
    if not _KEBAB_RE.match(v):
        raise ValueError(
            "skill_name must be lowercase kebab-case (e.g. 'my-skill-name')"
        )
    return v


def _validate_description(v: str) -> str:
    if len(v) > _MAX_DESC_LEN:
        raise ValueError(f"description must be at most {_MAX_DESC_LEN} characters")
    return v


class SkillCreateSchema(BaseModel):
    skill_name: str
    description: str
    instructions: str = ""
    references: dict[str, str] | None = None
    assets: dict[str, str] | None = None
    is_public: bool = False
    project_id: str | None = "thaink2"

    @field_validator("skill_name")
    @classmethod
    def check_skill_name(cls, v: str) -> str:
        return _validate_skill_name(v)

    @field_validator("description")
    @classmethod
    def check_description(cls, v: str) -> str:
        return _validate_description(v)


class SkillUpdateSchema(BaseModel):
    skill_name: str | None = None
    description: str | None = None
    instructions: str | None = None
    references: dict[str, str] | None = None
    assets: dict[str, str] | None = None
    is_public: bool | None = None

    @field_validator("skill_name")
    @classmethod
    def check_skill_name(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_skill_name(v)
        return v

    @field_validator("description")
    @classmethod
    def check_description(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_description(v)
        return v
