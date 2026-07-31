"""Pydantic schemas for Data Lake and Data Handler endpoints."""

from typing import Any, Optional

from pydantic import BaseModel


class BoardConfigSchema(BaseModel):
    """Configuration for creating/connecting to a storage board."""

    bucket_name: str
    prefix: str
    storage_source: str = "s3"
    region: Optional[str] = None
    endpoint_url: Optional[str] = None


class PinWriteSchema(BaseModel):
    """Schema for writing a pin to the board."""

    bucket_name: str
    prefix: str
    storage_source: str = "s3"
    pin_name: str
    data: list[dict[str, Any]]
    pin_type: str = "parquet"


class PinReadSchema(BaseModel):
    """Schema for reading a pin (used when query params are not enough)."""

    bucket_name: str
    prefix: str
    storage_source: str = "s3"


class FilterConditionSchema(BaseModel):
    """Schema for a single filter condition."""

    field: str
    operator: str  # "==", "!=", ">", "<", ">=", "<=", "in", "not_in", "contains"
    value: Any


class AggregationConfigSchema(BaseModel):
    """Schema for a single aggregation configuration."""

    field: str
    operation: str  # "sum", "mean", "count", "max", "min", "std"
    group_by: Optional[str] = None


class DataProcessSchema(BaseModel):
    """Schema for processing a dataset (filter + aggregate)."""

    dataset: list[dict[str, Any]]
    filter_conditions: Optional[list[FilterConditionSchema]] = None
    aggregations: Optional[list[AggregationConfigSchema]] = None
    match_all: bool = True


class DataLakeProcessSchema(BaseModel):
    """Schema for processing data directly from a data lake pin."""

    bucket_name: str
    prefix: str
    storage_source: str = "s3"
    pin_name: str
    filter_conditions: Optional[list[FilterConditionSchema]] = None
    aggregations: Optional[list[AggregationConfigSchema]] = None
    match_all: bool = True
