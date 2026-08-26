# SPDX-License-Identifier: FSL-1.1-ALv2
"""`GENERATION_TIER` and the self-hosted model settings (design §11.5.4, §13.1).

WHY THIS FILE EXISTS
--------------------
`core/config.py` cannot import `src.ai` — `TID251` bans a cross-domain import from the lowest
layer, and correctly, because every module that reads settings would pay for the cycle. So
`generation_tier`'s six permitted values are RESTATED in the `Literal` rather than read from
`ModelTier`, which is not this repository's usual preference and invites the drift the preference
exists to prevent.

This is the guard that closes it. A tier added to `ModelTier` and not to the `Literal` would become
quietly unconfigurable — the enum would have seven members and one of them could never be selected —
and the failure would look like a configuration bug rather than a missing line.
"""

from __future__ import annotations

import typing

import pytest
from src.ai.routing.tiers import ModelTier
from src.core.config import PROJECT_CONFIG_KEYS, Settings, get_settings, load_project_dotenv

BASE = {
    "DATABASE_URL": "postgresql+asyncpg://forgeops@127.0.0.1:1/forgeops",
    "REDIS_URL": "redis://127.0.0.1:1/0",
}


def _settings(**overrides: str) -> Settings:
    return get_settings({**BASE, **overrides})


def test_the_literal_permits_exactly_the_model_tier_values() -> None:
    """The drift guard. Read from BOTH sources and compared, so neither can move alone."""
    permitted = set(typing.get_args(Settings.model_fields["generation_tier"].annotation))
    assert permitted == {tier.value for tier in ModelTier}, (
        "GENERATION_TIER's Literal and ModelTier disagree. A tier in the enum but not the Literal "
        "cannot be configured; one in the Literal but not the enum cannot be routed to."
    )


@pytest.mark.parametrize("tier", [tier.value for tier in ModelTier])
def test_every_real_tier_is_configurable(tier: str) -> None:
    assert _settings(GENERATION_TIER=tier).generation_tier == tier


def test_an_unknown_tier_is_refused_at_load_rather_than_at_request_time() -> None:
    """`ModelRouter.complete` answers EXHAUSTED for an absent tier, which looks like an outage.

    A misspelled `GENERATION_TIER` would therefore make every run fall back to a template and read
    as every endpoint being down. Refusing to load names the problem instead.
    """
    with pytest.raises(ValueError, match="generation_tier"):
        _settings(GENERATION_TIER="high_reasoning")


def test_the_default_is_the_tier_a_fresh_clone_can_actually_reach() -> None:
    """`self_hosted`, because it is the only chain whose endpoints need no hosted key.

    Every other tier's primary resolves to a provider whose `LLM_KEY_*` is a placeholder in
    `.env.example`, so defaulting to `high_coding` would make a fresh clone's every generation run a
    template fallback while the configuration claimed otherwise.
    """
    assert _settings().generation_tier == "self_hosted"


class TestTheSelfHostedModelSettingsAreDeclaredAndShipped:
    """A key `Settings` accepts and `.env.example` omits is a key no deployment sets."""

    @pytest.mark.parametrize(
        "key",
        [
            "SELF_HOSTED_BASE_URL",
            "SELF_HOSTED_MODEL_ID",
            "SELF_HOSTED_EMBEDDING_MODEL_ID",
            "OLLAMA_PORT",
            "GENERATION_TIER",
        ],
    )
    def test_the_key_is_registered_and_committed(self, key: str) -> None:
        assert key in PROJECT_CONFIG_KEYS, f"{key} is not in PROJECT_CONFIG_KEYS, so a dotenv carrying it fails to load"
        baseline = load_project_dotenv((".env.example",))
        assert key in baseline, f"{key} is missing from .env.example, so a fresh clone does not set it"

    def test_the_baseline_points_the_self_hosted_endpoint_at_the_compose_service(self) -> None:
        """It used to name `host.docker.internal`, which resolves only on Docker Desktop.

        There was no self-hosted service in the topology then, so the value pointed at a server
        nobody was running. `docker-compose.yml` now declares one.
        """
        baseline = load_project_dotenv((".env.example",))
        assert baseline["SELF_HOSTED_BASE_URL"] == "http://ollama:11434/v1"

    def test_the_baseline_names_a_model_and_an_embedding_model(self) -> None:
        """`${SELF_HOSTED_MODEL_ID}` is expanded by `load_tier_config` and an unset value is fatal.

        So an empty baseline value is not a soft default — it is a backend that refuses to start.
        """
        baseline = load_project_dotenv((".env.example",))
        assert baseline["SELF_HOSTED_MODEL_ID"].strip()
        assert baseline["SELF_HOSTED_EMBEDDING_MODEL_ID"].strip()

    def test_the_embedding_model_carries_an_explicit_tag(self) -> None:
        """The container healthcheck greps `ollama list`, which always prints a tag.

        A bare `nomic-embed-text` would never match `nomic-embed-text:latest` and the service would
        stay unhealthy for ever with both models present.
        """
        baseline = load_project_dotenv((".env.example",))
        assert ":" in baseline["SELF_HOSTED_EMBEDDING_MODEL_ID"]

    def test_the_settings_read_the_baseline_values(self) -> None:
        baseline = load_project_dotenv((".env.example",))
        settings = _settings(
            SELF_HOSTED_BASE_URL=baseline["SELF_HOSTED_BASE_URL"],
            SELF_HOSTED_MODEL_ID=baseline["SELF_HOSTED_MODEL_ID"],
            SELF_HOSTED_EMBEDDING_MODEL_ID=baseline["SELF_HOSTED_EMBEDDING_MODEL_ID"],
        )
        assert settings.self_hosted_base_url == baseline["SELF_HOSTED_BASE_URL"]
        assert settings.self_hosted_model_id == baseline["SELF_HOSTED_MODEL_ID"]
        assert settings.self_hosted_embedding_model_id == baseline["SELF_HOSTED_EMBEDDING_MODEL_ID"]
