from src.secrets.redaction import create_redacted_chunk, create_redacted_instruction


def _make_secret(prefix: str, suffix: str) -> str:
    return f"{prefix}{suffix}"


def test_redaction_patterns():
    # Tests a pattern (e.g. AWS key or ghp_ token)
    token = _make_secret("ghp_", "123456789012345678901234567890123456")
    raw = f"Here is my token: {token}"
    chunk = create_redacted_chunk(raw)
    assert "ghp_" not in chunk

    assert "FORGEOPS_REDACTED" in chunk


def test_project_secrets():
    # Tests specific project secrets injected
    raw = "My secret password is supersecret"
    inst = create_redacted_instruction(raw, project_secrets=["supersecret"])
    assert "supersecret" not in inst
    assert "FORGEOPS_REDACTED" in inst
