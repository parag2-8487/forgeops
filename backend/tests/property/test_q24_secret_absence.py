# SPDX-License-Identifier: Apache-2.0
from hypothesis import given, settings
from hypothesis import strategies as st
from src.secrets.redaction import create_redacted_chunk


def _make_secret(prefix: str, suffix: str) -> str:
    return f"{prefix}{suffix}"


def _ghp_token() -> str:
    return _make_secret("ghp_", "1234567890abcdef1234567890abcdef123456")


def _akia_key() -> str:
    return _make_secret("AKIA", "1234567890ABCDEF")


@settings(max_examples=100)
@given(secret=st.sampled_from([_ghp_token(), _akia_key()]), surrounding_text=st.text(min_size=1, max_size=50))
def test_q24_secret_absence(secret: str, surrounding_text: str):
    """
    Property Q-24: Secret absence in logs and audit payloads.
    Redaction chokepoint must eliminate high-risk credential patterns.
    """
    raw_payload = f"{surrounding_text} {secret} {surrounding_text}"
    redacted_payload = create_redacted_chunk(raw_payload)

    assert secret not in redacted_payload
    assert "FORGEOPS_REDACTED_PATTERN" in redacted_payload
