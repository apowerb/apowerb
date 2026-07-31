from pydantic import BaseModel, field_validator


class ApiKeyCreateSchema(BaseModel):
    """Schema for creating a saved API key."""

    key_name: str  # "Ma cle Anthropic"
    provider: str  # "anthropic", "mistral", "openai", "ovhcloud"
    api_key_value: str  # La valeur de la cle (sera chiffree)
    model: str | None = None
    model_api_base: str | None = None
    owner_id: str = ""
    organization_id: str = ""

    @field_validator("key_name")
    @classmethod
    def validate_key_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 100:
            raise ValueError("key_name must be 1-100 characters")
        return v

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("api_key_value")
    @classmethod
    def validate_api_key_value(cls, v: str) -> str:
        if not v or len(v) < 4 or len(v) > 500:
            raise ValueError("api_key_value must be 4-500 characters")
        return v
