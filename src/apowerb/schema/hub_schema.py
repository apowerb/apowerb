from pydantic import BaseModel


class HubPublishSchema(BaseModel):
    """Schema for publishing an agent to the Hub."""

    agent_id: str
    hub_name: str
    hub_description: str
    hub_tags: list[str] | None = None
    hub_category: str | None = "general"


class HubCloneSchema(BaseModel):
    """Schema for cloning an agent from the Hub."""

    hub_agent_id: str
    clone_name: str | None = None
