# SPDX-License-Identifier: Apache-2.0
import pytest
from src.generation.templates import TemplateLoader, TemplateManifest


def test_template_loader_success():
    loader = TemplateLoader()
    manifest = TemplateManifest(
        name="dockerfile-python",
        language="python",
        required_variables=["APP_PORT"]
    )
    raw_content = "FROM python:3.11\nEXPOSE {{APP_PORT}}"
    loader.register_template(manifest, raw_content)

    rendered = loader.render("dockerfile-python", {"APP_PORT": 8000})
    assert "EXPOSE 8000" in rendered


def test_template_loader_missing_variable():
    loader = TemplateLoader()
    manifest = TemplateManifest(
        name="dockerfile-python",
        language="python",
        required_variables=["APP_PORT"]
    )
    loader.register_template(manifest, "FROM python:3.11")

    with pytest.raises(ValueError, match="Missing required variable"):
        loader.render("dockerfile-python", {})
