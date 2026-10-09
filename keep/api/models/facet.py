from typing import Any, Optional
from pydantic import BaseModel
import pydantic

from keep.api.models.db.facet import FacetType

class FacetOptionsQueryDto(BaseModel):
    cel: Optional[str]
    facet_queries: Optional[dict[str, str]]

class FacetOptionDto(BaseModel):
    display_name: str
    value: Any
    matches_count: int

class FacetDto(BaseModel):
    id: str
    property_path: str
    name: str
    description: Optional[str]
    is_static: bool
    is_lazy: bool = True
    type: FacetType
    order: Optional[int] = None
    is_open_by_default: Optional[bool] = None

class CreateFacetDto(BaseModel):
    property_path: str
    name: str
    description: Optional[str] = None
    order: Optional[int] = None
    is_open_by_default: Optional[bool] = None

    @pydantic.validator('property_path')
    def name_validator(cls, v: str):
        if not v.strip():
            raise ValueError('property_path must not be empty')
        return v

    @pydantic.validator('name')
    def property_path_validator(cls, v: str):
        if not v.strip():
            raise ValueError('name must not be empty')
        return v

class UpdateFacetDto(BaseModel):
    name: Optional[str] = None
    property_path: Optional[str] = None
    description: Optional[str] = None
    order: Optional[int] = None
    is_open_by_default: Optional[bool] = None

