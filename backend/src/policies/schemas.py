import uuid
from typing import Any
from pydantic import BaseModel, Field

class PolicyCreate(BaseModel):
    name: str = Field(..., max_length=200)
    engine: str = Field(default="rego", max_length=16)
    rego_rules: str
    enabled: bool = Field(default=True)
    template_id: str | None = Field(default=None, max_length=64)

class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    rego_rules: str | None = None
    enabled: bool | None = None
    template_id: str | None = Field(default=None, max_length=64)

class PolicyRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    tenant_id: uuid.UUID | None
    name: str
    engine: str
    rego_rules: str
    enabled: bool
    template_id: str | None

class PolicyTemplateRead(BaseModel):
    id: str
    name: str
    description: str
    rego_rules: str
    parameters: dict[str, Any]

class DryRunInput(BaseModel):
    input: dict[str, Any]
