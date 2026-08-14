# SPDX-License-Identifier: Apache-2.0
from src.generation.template_library import DEFAULT_TEMPLATES, get_default_template_loader


def test_default_template_loader_all_languages():
    loader = get_default_template_loader()
    languages = ["nodejs", "python", "go", "rust", "java", "ruby", "php", "dotnet"]

    for lang_template in DEFAULT_TEMPLATES.keys():
        rendered = loader.render(lang_template, {"PORT": 8080})
        assert "8080" in rendered


def test_template_loader_registered_count():
    loader = get_default_template_loader()
    assert len(loader.templates) == 8
