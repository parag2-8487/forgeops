# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for vault profile configuration and key resolvers (Task 13.15).

Tests:
- Unprofiled compose shows exactly 5 default services (re-assertion)
- EnvKeyResolver works for both present and missing keys
- InfisicalKeyResolver is importable but requires network
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from src.ai.routing.keys import EnvKeyResolver, SecretValue

# --- Compose default services ---

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _get_default_services(compose_path: Path) -> list[str]:
    """Parse docker-compose.yml and return services without a 'profiles' key."""
    with open(compose_path) as f:
        data = yaml.safe_load(f)
    services = data.get("services", {})
    default = []
    for name, config in services.items():
        if "profiles" not in config:
            default.append(name)
    return sorted(default)


@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.yml not found")
def test_unprofiled_compose_matches_the_committed_default_service_list():
    """The unprofiled set is exactly what `scripts/compose-default-services.txt` lists.

    Was `len(defaults) == 5` plus a literal five-name list — the fourth copy of the same
    data in this repository, alongside `check-compose-validate.py`,
    `test_dockerfile_compose.py` and the data file itself. Task 6.3 promoted two services
    and broke three of the four. Reading the one source means a promotion is a one-line
    diff next to the task that makes it, which is what the data file's own header always
    claimed.
    """
    listed = sorted(
        line.strip()
        for line in (COMPOSE_PATH.parent / "scripts" / "compose-default-services.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert listed, "the default-service list is empty; an empty expectation proves nothing"
    assert _get_default_services(COMPOSE_PATH) == listed


@pytest.mark.skipif(not COMPOSE_PATH.exists(), reason="docker-compose.yml not found")
def test_vault_profile_service_exists():
    """The infisical service exists under the 'vault' profile."""
    with open(COMPOSE_PATH) as f:
        data = yaml.safe_load(f)
    services = data.get("services", {})
    assert "infisical" in services, "infisical service not found in compose"
    infisical = services["infisical"]
    assert "profiles" in infisical
    assert "vault" in infisical["profiles"]
    assert infisical["image"].startswith("infisical/infisical:")


# --- EnvKeyResolver tests ---


def test_env_key_resolver_present_key():
    """EnvKeyResolver resolves a key that is set in the environment."""
    with patch.dict(os.environ, {"LLM_KEY_OPENAI": "sk-test-12345"}):
        resolver = EnvKeyResolver()
        result = resolver.resolve("openai")
        assert result is not None
        assert isinstance(result, SecretValue)
        assert result.get_secret_value() == "sk-test-12345"


def test_env_key_resolver_missing_key():
    """EnvKeyResolver returns None for a missing key."""
    env = os.environ.copy()
    env.pop("LLM_KEY_NONEXIST", None)
    with patch.dict(os.environ, env, clear=True):
        resolver = EnvKeyResolver()
        result = resolver.resolve("nonexist")
        assert result is None


def test_env_key_resolver_empty_key():
    """EnvKeyResolver returns None for an empty-string key."""
    with patch.dict(os.environ, {"LLM_KEY_EMPTY": "   "}):
        resolver = EnvKeyResolver()
        result = resolver.resolve("empty")
        assert result is None


def test_env_key_resolver_custom_prefix():
    """EnvKeyResolver works with a custom prefix."""
    with patch.dict(os.environ, {"MY_PREFIX_ANTHROPIC": "ant-key-xyz"}):
        resolver = EnvKeyResolver(prefix="MY_PREFIX_")
        result = resolver.resolve("anthropic")
        assert result is not None
        assert result.get_secret_value() == "ant-key-xyz"


def test_secret_value_repr_hides_value():
    """SecretValue repr never exposes the actual secret."""
    secret = SecretValue("super-secret-key")
    assert "super-secret-key" not in repr(secret)
    assert "super-secret-key" not in str(secret)
    assert secret.get_secret_value() == "super-secret-key"


# --- InfisicalKeyResolver importability ---


def test_infisical_key_resolver_importable():
    """InfisicalKeyResolver module is importable (even if network isn't available).

    The resolver itself requires network connectivity to function, so we only
    test that the import path and class structure is valid.
    """
    try:
        from src.ai.routing.keys import InfisicalKeyResolver  # type: ignore[attr-defined]

        # If it exists, verify it has the resolve method
        assert hasattr(InfisicalKeyResolver, "resolve")
    except (ImportError, AttributeError):
        # InfisicalKeyResolver not yet implemented — this is acceptable in Phase 0
        pytest.skip("InfisicalKeyResolver not yet implemented")
