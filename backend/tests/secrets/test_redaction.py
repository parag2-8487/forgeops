from src.secrets.redaction import create_redacted_chunk, create_redacted_instruction


def test_redaction_patterns():
    # Tests a pattern (e.g. AWS key or ghp_ token)
    raw = "Here is my token: ghp_123456789012345678901234567890123456"
    chunk = create_redacted_chunk(raw)
    assert "ghp_" not in chunk
    assert "FORGEOPS_REDACTED" in chunk


def test_project_secrets():
    # Tests specific project secrets injected
    raw = "My secret password is supersecret"
    inst = create_redacted_instruction(raw, project_secrets=["supersecret"])
    assert "supersecret" not in inst
    assert "FORGEOPS_REDACTED" in inst
