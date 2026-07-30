# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Phase 1 configuration surface (design.md §7.1, §13.1, §17.1 D-39).

Phase 1 adds fields; it changes no mechanism. So these tests assert both halves:

* the new fields exist, carry the §13.1 bounds, and every relational rule between
  them holds (overlap < target, renew < ttl, timeout > interval);
* the Phase 0 contracts still hold over the larger surface — `extra="forbid"` for
  project sources, all errors accumulated into ONE report (P-15), and unrelated
  ambient OS variables still ignored.

The last one matters more as the surface grows: with 90 fields, a settings class that
started reading arbitrary environment variables would be very hard to notice.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from src.core.config import PROJECT_CONFIG_KEYS, Settings, get_settings, load_project_dotenv

pytestmark = pytest.mark.mandatory

BASE = {
    "database_url": "postgresql+asyncpg://forgeops:pw@localhost:5432/forgeops",
    "redis_url": "redis://localhost:6379/0",
}


def build(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


class TestTheCommittedBaselineIsComplete:
    """`.env.example` and `PROJECT_CONFIG_KEYS` must agree exactly.

    Not "the baseline is a subset". A key in the inventory but missing from the file
    is a value a fresh clone cannot supply; a key in the file but not the inventory is
    rejected outright by `load_project_dotenv`. Either direction is a fresh-clone
    failure, so both are asserted.
    """

    def test_the_baseline_parses_and_declares_only_registered_keys(self) -> None:
        parsed = load_project_dotenv((".env.example",))
        assert sorted(k for k in parsed if k not in PROJECT_CONFIG_KEYS) == []

    def test_every_registered_key_appears_in_the_baseline(self) -> None:
        parsed = load_project_dotenv((".env.example",))
        assert sorted(k for k in PROJECT_CONFIG_KEYS if k not in parsed) == []

    def test_settings_builds_from_the_baseline_alone(self) -> None:
        """The fresh-clone guarantee: no committed `.env` needed (design §13.3)."""
        settings = get_settings(load_project_dotenv((".env.example",)))
        assert settings.oidc_app_audience == "forgeops-api"
        assert settings.embedding_backend == "voyage"
        assert settings.task_dispatcher == "arq"

    def test_the_app_audience_is_distinct_from_the_gateway_audience(self) -> None:
        """A token minted for the MCP gateway must not be replayable at the app API."""
        settings = get_settings(load_project_dotenv((".env.example",)))
        assert settings.oidc_app_audience != settings.mcp_oidc_audience


class TestTheIterationBoundIsNotTunable:
    """`generation_max_iterations: Literal[3]` (§7.1, Q-08)."""

    def test_the_default_is_three(self) -> None:
        assert build().generation_max_iterations == 3

    def test_the_string_form_an_env_var_arrives_as_is_accepted(self) -> None:
        assert build(generation_max_iterations="3").generation_max_iterations == 3

    @pytest.mark.parametrize("value", [1, 2, 4, 10, 0, -1, "10", "0", "", "three", None])
    def test_no_other_value_loads(self, value: object) -> None:
        """Typed as int an operator could set 10 and move a safety bound quietly."""
        with pytest.raises(ValidationError):
            build(generation_max_iterations=value)


class TestRelationalRules:
    def test_overlap_must_be_below_target(self) -> None:
        with pytest.raises(ValidationError, match="CHUNK_OVERLAP_TOKENS"):
            build(chunk_target_tokens=256, chunk_overlap_tokens=256)

    def test_overlap_equal_to_target_is_rejected_not_merely_greater(self) -> None:
        """Equality is the non-terminating case too, so the bound is strict."""
        with pytest.raises(ValidationError):
            build(chunk_target_tokens=512, chunk_overlap_tokens=512)

    def test_a_valid_overlap_is_accepted(self) -> None:
        assert build(chunk_target_tokens=512, chunk_overlap_tokens=511).chunk_overlap_tokens == 511

    def test_renewal_must_precede_certificate_expiry(self) -> None:
        with pytest.raises(ValidationError, match="DEVICE_CERT_RENEW_BEFORE_HOURS"):
            build(device_cert_ttl_hours=6, device_cert_renew_before_hours=6)

    def test_heartbeat_timeout_must_exceed_the_interval(self) -> None:
        with pytest.raises(ValidationError, match="HEARTBEAT_TIMEOUT_SECONDS"):
            build(heartbeat_interval_seconds=30, heartbeat_timeout_seconds=30)

    def test_the_shipped_heartbeat_pair_is_the_designed_30_90(self) -> None:
        settings = build()
        assert (settings.heartbeat_interval_seconds, settings.heartbeat_timeout_seconds) == (30, 90)


class TestThePairingAlphabet:
    def test_the_default_is_crockford_base32(self) -> None:
        alphabet = build().pairing_code_alphabet
        assert len(alphabet) == 32
        assert not set("ILOU") & set(alphabet)

    @pytest.mark.parametrize("bad", ["0123456789ABCDEFGHIJKLMNOPQRSTUV", "0123456789ABCDEFO"])
    def test_an_ambiguous_alphabet_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="PAIRING_CODE_ALPHABET"):
            build(pairing_code_alphabet=bad)

    def test_duplicates_are_rejected(self) -> None:
        """Duplicates silently reduce the entropy the code is assumed to carry."""
        with pytest.raises(ValidationError, match="duplicate"):
            build(pairing_code_alphabet="00123456789ABCDEFGHJKMNPQRSTVWXYZ")

    def test_too_few_symbols_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="entropy"):
            build(pairing_code_alphabet="0123456789")


class TestBlastRadiusIsDemoted:
    """D-39 / Q-30: the env var is a development default, never production authority."""

    def test_its_presence_is_a_startup_error_in_production(self) -> None:
        with pytest.raises(ValidationError, match="MCP_AGENT_BLAST_RADIUS"):
            build(app_env="production", mcp_oidc_issuers="https://issuer.test", mcp_agent_blast_radius="read_only")

    def test_even_the_narrowest_value_is_refused_in_production(self) -> None:
        """The PRESENCE is the error, not the value.

        Refusing only wide values would still let an operator set it, and would make
        the rule depend on reading the value correctly rather than on the variable
        having no production role at all.
        """
        with pytest.raises(ValidationError, match="MCP_AGENT_BLAST_RADIUS"):
            build(
                app_env="production",
                mcp_oidc_issuers="https://issuer.test",
                mcp_agent_blast_radius="read_only",
            )

    def test_it_remains_usable_outside_production(self) -> None:
        assert build(app_env="development", mcp_agent_blast_radius="workspace").mcp_agent_blast_radius == "workspace"

    def test_production_without_it_still_gets_the_safe_default(self) -> None:
        settings = build(
            app_env="production",
            mcp_oidc_issuers="https://issuer.test",
            oidc_issuer="https://issuer.test",
            oidc_client_id="forgeops",
            oidc_client_secret="change-me-locally",
            envelope_pepper="change-me-locally",
            internal_ca_cert_pem="pem",
            internal_ca_key_pem="pem",
        )
        assert settings.mcp_agent_blast_radius == "read_only"


class TestProductionSecretsAreReportedTogether:
    """P-15's accumulate-all-errors contract, applied to the new credentials."""

    def test_every_missing_credential_appears_in_one_report(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            build(app_env="production", mcp_oidc_issuers="https://issuer.test")
        message = str(excinfo.value)
        for name in (
            "OIDC_ISSUER",
            "OIDC_CLIENT_ID",
            "OIDC_CLIENT_SECRET",
            "ENVELOPE_PEPPER",
            "INTERNAL_CA_CERT_PEM",
            "INTERNAL_CA_KEY_PEM",
        ):
            assert name in message, f"{name} was not reported"

    def test_the_local_seal_key_is_required_only_for_the_local_backend(self) -> None:
        common = dict(
            app_env="production",
            mcp_oidc_issuers="https://issuer.test",
            oidc_issuer="https://issuer.test",
            oidc_client_id="forgeops",
            oidc_client_secret="change-me-locally",
            envelope_pepper="change-me-locally",
            internal_ca_cert_pem="pem",
            internal_ca_key_pem="pem",
        )
        # infisical backend: no seal key needed.
        assert build(**common, secret_backend="infisical").secret_backend == "infisical"
        with pytest.raises(ValidationError, match="LOCAL_SECRET_SEAL_KEY"):
            build(**common, secret_backend="local")

    def test_development_needs_none_of_them(self) -> None:
        assert build(app_env="development").oidc_client_secret.get_secret_value() == ""


class TestSecretsAreNotRenderable:
    """A SecretStr must not leak through repr or str, which is where logs get them."""

    @pytest.mark.parametrize(
        "field",
        [
            "oidc_client_secret",
            "envelope_pepper",
            "internal_ca_key_pem",
            "local_secret_seal_key",
            "llm_key_voyage",
            "authentik_bootstrap_token",
        ],
    )
    def test_the_value_is_not_in_the_rendered_form(self, field: str) -> None:
        marker = "test-only-not-a-real-secret"
        settings = build(**{field: marker})
        assert marker not in repr(getattr(settings, field))
        assert marker not in str(getattr(settings, field))
        assert marker not in repr(settings)
        assert getattr(settings, field).get_secret_value() == marker


class TestPhase0MechanismsStillHold:
    def test_an_unknown_key_is_still_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            build(totally_unknown_setting="x")

    def test_unrelated_ambient_variables_are_still_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With 90 fields, silently reading the ambient environment would be hard to spot."""
        for name, value in {
            "PATH": "/nonsense",
            "HOME": "/nonsense",
            "CI": "true",
            "EDITOR": "vi",
            "LANG": "C",
            "PYTEST_CURRENT_TEST": "x",
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("DATABASE_URL", BASE["database_url"])
        monkeypatch.setenv("REDIS_URL", BASE["redis_url"])
        settings = get_settings()
        assert settings.database_url is not None

    def test_all_field_errors_are_accumulated_not_reported_one_at_a_time(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            build(
                session_ttl_seconds=1,
                pairing_code_max_attempts=99,
                device_cert_ttl_hours=0,
                retrieval_top_k=0,
            )
        assert len(excinfo.value.errors()) >= 4, excinfo.value.errors()
