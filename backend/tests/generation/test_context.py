import pytest
from src.generation.context import assemble_prompt
from src.secrets.redaction import create_redacted_chunk, create_redacted_instruction

def test_assemble_prompt_redaction():
    sys_chunk = create_redacted_chunk("system context")
    chunks = [create_redacted_chunk("chunk 1 text secret1", project_secrets=["secret1"])]
    inst = create_redacted_instruction("instruction secret2", project_secrets=["secret2"])
    
    prompt = assemble_prompt(system=sys_chunk, chunks=chunks, instruction=inst)
    
    assert "system context" in prompt
    assert "secret1" not in prompt
    assert "secret2" not in prompt
    assert "FORGEOPS_REDACTED_VALUE" in prompt
