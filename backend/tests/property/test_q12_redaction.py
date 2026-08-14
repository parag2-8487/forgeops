import pytest
from hypothesis import given
from hypothesis import strategies as st
from src.generation.context import assemble_prompt
from src.secrets.redaction import create_redacted_chunk, create_redacted_instruction

pytestmark = [pytest.mark.mandatory]


@given(
    system_text=st.text(min_size=1, max_size=100),
    chunk_texts=st.lists(st.text(min_size=1, max_size=100), min_size=1, max_size=5),
    instruction_text=st.text(min_size=1, max_size=100),
    secret_value=st.text(min_size=10, max_size=20, alphabet=st.characters(blacklist_categories=("C", "Z"))),
)
def test_q12_redaction_before_assembly(
    system_text: str, chunk_texts: list[str], instruction_text: str, secret_value: str
) -> None:
    # Ensure it's not accidentally a substring of the redaction marker
    secret_value = f"SECRET_{secret_value}_END"
    # Inject the secret value into the texts
    injected_system = f"{system_text} {secret_value} {system_text[::-1]}"
    injected_chunks = [f"{c} {secret_value} {c}" for c in chunk_texts]
    injected_instruction = f"{instruction_text} {secret_value}"

    project_secrets = [secret_value]

    # Redact
    system_redacted = create_redacted_chunk(injected_system, project_secrets)
    chunks_redacted = [create_redacted_chunk(c, project_secrets) for c in injected_chunks]
    instruction_redacted = create_redacted_instruction(injected_instruction, project_secrets)

    # Assemble
    prompt = assemble_prompt(system=system_redacted, chunks=chunks_redacted, instruction=instruction_redacted)

    # Verify the secret is completely absent
    assert secret_value not in prompt, "Secret leaked into the assembled prompt!"
    assert "FORGEOPS_REDACTED_VALUE" in prompt
