from pydantic import BaseModel, EmailStr


class ToolConfigCreateSchema(BaseModel):
    """Schema for creating a new tool_config."""

    tool_config_name: str
    tool_name: str
    tool_config_params: dict | None = None
    tool_category: str
    organization_id: str
    status: str | None = "active"
    tool_config_type: str | None = "active"
    project_id: str | None = "thaink2"
    owner_id: str
    tags: list[str] | None = None
