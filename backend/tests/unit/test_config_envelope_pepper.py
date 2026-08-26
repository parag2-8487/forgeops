# SPDX-License-Identifier: FSL-1.1-ALv2
"""An empty `ENVELOPE_PEPPER` must be refused when `Settings` is built, in every environment.

WHY THIS IS NOT MERELY "ANOTHER REQUIRED SETTING"

`credentials.md` said "the backend refuses to start without it". That was the intended behaviour and
not the actual one: `envelope_pepper` defaulted to `SecretStr("")` and the only check was inside
`_require_production_secrets`, which returns early unless `APP_ENV=production`. So on every other
environment the process started, answered `/health/live`, and then died on the first pairing attempt
— in one of FOUR different voices, depending on which of `DeviceService`,
`derive_key_encryption_key`, `auth/sessions.py` or `GovernanceChokepoint` happened to touch the
pepper first.

WHAT AN EMPTY PEPPER ACTUALLY DOES, which is why "fatal later" is not good enough:

* `HMAC-SHA256` under an empty key still computes. Device tokens and pairing codes would be stored
  under an unkeyed digest, so anyone able to read `agent_devices` could forge the stored HMAC for any
  device by hashing a value of their own choosing.
* `derive_key_encryption_key` HKDFs the pepper (D-62), so an empty pepper derives the SAME
  key-encryption key everywhere. `agent_devices.envelope_key_enc` from one installation would unseal
  in another, while the column name still asserted ciphertext.

Neither fails loudly at the point of use. That is the argument for the boundary.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from src.core.config import Settings

from tests.synthetic_secrets import postgres_dsn

pytestmark = [pytest.mark.mandatory]

#: The minimum a `Settings` needs besides the pepper. Kept next to the tests so a future required
#: field surfaces here as a clear error rather than as a confusing pass.
BASE = {
    "database_url": postgres_dsn(),
    "redis_url": "redis://localhost:6379/0",
}


class TestAnEmptyPepperIsRefused:
    @pytest.mark.parametrize("value", ["", "   ", "\t", "\n"])
    def test_empty_or_whitespace_is_refused(self, value: str) -> None:
        """Whitespace counts as empty: `.strip()`, because a pepper of spaces is not a key."""
        with pytest.raises(ValidationError) as caught:
            Settings(**BASE, envelope_pepper=SecretStr(value))
        assert "ENVELOPE_PEPPER" in str(caught.value)

    def test_the_message_says_why_rather_than_only_that(self) -> None:
        """An operator who reads "must be non-empty" learns nothing about the risk.

        The message names both consequences, because a reader who thinks this is a formality will
        set it to a single character and move on.
        """
        with pytest.raises(ValidationError) as caught:
            Settings(**BASE, envelope_pepper=SecretStr(""))
        message = str(caught.value)
        assert "HMAC" in message
        assert "identical in every deployment" in message

    @pytest.mark.parametrize("app_env", ["development", "test"])
    def test_it_is_refused_outside_production(self, app_env: str) -> None:
        """THE GAP THIS CLOSES. `_require_production_secrets` returns early for both of these, so
        before this validator existed an empty pepper was accepted here and only failed later, at
        the first pairing attempt, in whichever component touched it first.

        `staging` is deliberately absent: `app_env` is constrained to
        `development|test|production`, so a test naming a fourth value would assert a pattern error
        rather than anything about the pepper."""
        with pytest.raises(ValidationError) as caught:
            Settings(**BASE, app_env=app_env, envelope_pepper=SecretStr(""))
        assert "ENVELOPE_PEPPER" in str(caught.value)

    def test_it_is_refused_in_production_too(self) -> None:
        """Asserted separately, and WITHOUT claiming which message wins.

        In production a field validator on `MCP_OIDC_ISSUERS` raises before this model validator is
        reached, so the surfaced error is about that instead. Insisting on the pepper's message here
        would be asserting pydantic's validator ordering, which is not the property under test --
        the property is that an empty pepper is never ACCEPTED."""
        with pytest.raises(ValidationError):
            Settings(**BASE, app_env="production", envelope_pepper=SecretStr(""))

    def test_the_refusal_names_the_variable_an_operator_must_set(self) -> None:
        """Not the field name. `envelope_pepper` is the Python attribute; `ENVELOPE_PEPPER` is what
        goes in `.env`, and an operator reading a boot failure needs the second one."""
        with pytest.raises(ValidationError) as caught:
            Settings(**BASE, envelope_pepper=SecretStr("   "))
        message = str(caught.value)
        assert "ENVELOPE_PEPPER" in message
        assert ".env" in message


class TestAValidPepperIsAccepted:
    def test_a_non_empty_pepper_constructs(self) -> None:
        """The check must not be so eager that a real configuration cannot boot."""
        settings = Settings(**BASE, envelope_pepper=SecretStr("a-development-pepper"))
        assert settings.envelope_pepper.get_secret_value() == "a-development-pepper"

    def test_the_baseline_ships_one(self) -> None:
        """`.env.example` must carry a value, or a fresh clone cannot start at all.

        Read from the file rather than from `Settings`, because `Settings` sets `env_file=None` and
        would not see it — so this asserts the thing a developer actually copies.
        """
        from pathlib import Path

        baseline = Path(__file__).resolve().parents[3] / ".env.example"
        declared = [
            line for line in baseline.read_text(encoding="utf-8").splitlines() if line.startswith("ENVELOPE_PEPPER=")
        ]
        assert len(declared) == 1, f"ENVELOPE_PEPPER is declared {len(declared)} time(s) in .env.example"
        value = declared[0].split("=", 1)[1].split("#")[0].strip()
        assert value, ".env.example declares ENVELOPE_PEPPER with an empty value, so a fresh clone cannot boot"
