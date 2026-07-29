"""
Pydantic schemas for text-to-SQL tool input/output validation.
"""
from pydantic import BaseModel, Field
from typing import Optional


class TextToSQLInput(BaseModel):
    """Schema for text-to-SQL tool input."""
    question: str = Field(..., description="Natural language question about the data")
    max_rows: int = Field(default=100, ge=1, le=1000, description="Maximum number of rows to return")
    include_sql: bool = Field(default=True, description="Whether to include generated SQL in response")


class GetDatabaseSchemaInput(BaseModel):
    """Schema for get_database_schema tool input (no parameters needed)."""
    pass


class TextToSQLExplainInput(BaseModel):
    """Schema for text-to-SQL explain tool input."""
    question: str = Field(..., description="Natural language question to convert to SQL")