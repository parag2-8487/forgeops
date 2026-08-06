# SPDX-License-Identifier: Apache-2.0
from hypothesis import given, settings, strategies as st
from src.generation.template_library import get_default_template_loader, DEFAULT_TEMPLATES


@settings(max_examples=100)
@given(
    template_name=st.sampled_from(list(DEFAULT_TEMPLATES.keys())),
    port=st.integers(min_value=1024, max_value=65535)
)
def test_q21_template_validity(template_name: str, port: int):
    """
    Property Q-21: Default template rendering completeness & validity.
    Rendering any registered template with valid parameter inputs must never contain
    unresolved mustache {{VAR}} placeholders.
    """
    loader = get_default_template_loader()
    rendered = loader.render(template_name, {"PORT": port})

    assert f"{port}" in rendered
    assert "{{" not in rendered
    assert "}}" not in rendered
