# SPDX-License-Identifier: Apache-2.0
from hypothesis import given, settings, strategies as st
from src.secrets.redaction import create_redacted_chunk


@settings(max_examples=100)
@given(
    secret=st.sampled_from(["ghp_1234567890abcdef1234567890abcdef123456", "AKIA1234567890ABCDEF"]),
    surrounding_text=st.text(min_size=1, max_size=50)
)
def test_q24_secret_absence(secret: str, surrounding_text: str):
    """
    Property Q-24: Secret absence in logs and audit payloads.
    Redaction chokepoint must eliminate high-risk credential patterns.
    """
    raw_payload = f"{surrounding_text} {secret} {surrounding_text}"
    redacted_payload = create_redacted_chunk(raw_payload)

    assert secret not in redacted_payload
    assert "FORGEOPS_REDACTED_PATTERN" in redacted_payload
