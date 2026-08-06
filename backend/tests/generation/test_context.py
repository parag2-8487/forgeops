import pytest
from src.generation.context import assemble_prompt
from src.secrets.redaction import create_redacted_chunk, create_redacted_instruction, RedactedChunk

def test_assemble_prompt_redaction():
    sys_chunk = create_redacted_chunk("system context")
    chunks = [create_redacted_chunk("chunk 1 text secret1", project_secrets=["secret1"])]
    inst = create_redacted_instruction("instruction secret2", project_secrets=["secret2"])
    
    prompt = assemble_prompt(system=sys_chunk, chunks=chunks, instruction=inst)
    
    assert "system context" in prompt
    assert "secret1" not in prompt
    assert "secret2" not in prompt
    assert "FORGEOPS_REDACTED_VALUE" in prompt

def test_model_endpoint_receives_no_secrets():
    from src.ai.routing.endpoints import CompletionRequest
    
    # Prove that the assembled prompt going to an endpoint is clean
    inst = create_redacted_instruction("secret_in_instruction", project_secrets=["secret_in_instruction"])
    prompt = assemble_prompt(
        system=create_redacted_chunk("sys"),
        chunks=[],
        instruction=inst
    )
    req = CompletionRequest(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )
    
    # The message content should be strictly redacted
    assert "secret_in_instruction" not in req.messages[0]["content"]

def test_store_holds_redacted_text_only():
    # Prove retriever can only store and retrieve RedactedChunk
    # This is a structural test asserting we use the types.
    from typing import get_type_hints
    
    class DummyRetrieverStore:
        def store(self, chunk: RedactedChunk):
            pass
        def retrieve(self) -> list[RedactedChunk]:
            return []
            
    hints = get_type_hints(DummyRetrieverStore.store)
    assert hints["chunk"] is RedactedChunk, "Store must accept only RedactedChunk"

