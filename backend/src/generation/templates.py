# SPDX-License-Identifier: Apache-2.0
from typing import Any

from pydantic import BaseModel, Field


class TemplateManifest(BaseModel):
    name: str
    language: str
    framework: str | None = None
    version: str = "1.0.0"
    required_variables: list[str] = Field(default_factory=list)


class TemplateLoader:
    def __init__(self, templates: dict[str, dict[str, Any]] | None = None):
        self.templates = templates or {}

    def register_template(self, manifest: TemplateManifest, raw_content: str) -> None:
        self.templates[manifest.name] = {"manifest": manifest, "content": raw_content}

    def render(self, name: str, variables: dict[str, Any]) -> str:
        if name not in self.templates:
            raise KeyError(f"Template '{name}' not found")

        item = self.templates[name]
        manifest: TemplateManifest = item["manifest"]
        content: str = item["content"]

        for var in manifest.required_variables:
            if var not in variables:
                raise ValueError(f"Missing required variable '{var}' for template '{name}'")

        rendered = content
        for k, v in variables.items():
            rendered = rendered.replace(f"{{{{{k}}}}}", str(v))

        return rendered
