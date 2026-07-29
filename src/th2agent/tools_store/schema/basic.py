from pydantic import BaseModel


class BasicToolSchemaInput(BaseModel):
    """Schema for basic tool input."""

    input_data: str


class BasicToolSchemaOutput(BaseModel):
    """Schema for basic tool output."""

    output_data: str
