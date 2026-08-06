from src.secrets.redaction import RedactedChunk, RedactedInstruction, RedactedPrompt

def assemble_prompt(*, system: RedactedChunk, chunks: list[RedactedChunk], instruction: RedactedInstruction) -> RedactedPrompt:
    """Assembles a final prompt strictly from already-redacted pieces."""
    combined = system + "\n\n"
    for c in chunks:
        combined += c + "\n\n"
    combined += instruction
    return RedactedPrompt(combined)
