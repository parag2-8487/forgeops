import re
from typing import NewType

RedactedChunk = NewType("RedactedChunk", str)
RedactedInstruction = NewType("RedactedInstruction", str)
RedactedPrompt = NewType("RedactedPrompt", str)

def _redact_text(text: str, project_secrets: list[str] | None = None) -> str:
    # Very simple pattern matching for common tokens and explicit secrets
    result = text
    if project_secrets:
        for secret in project_secrets:
            if secret:
                result = result.replace(secret, "FORGEOPS_REDACTED_VALUE")
    
    # Generic patterns
    result = re.sub(r"ghp_[a-zA-Z0-9]{36}", "FORGEOPS_REDACTED_PATTERN", result)
    result = re.sub(r"AKIA[A-Z0-9]{16}", "FORGEOPS_REDACTED_PATTERN", result)
    return result

def create_redacted_chunk(text: str, project_secrets: list[str] | None = None) -> RedactedChunk:
    return RedactedChunk(_redact_text(text, project_secrets))

def create_redacted_instruction(text: str, project_secrets: list[str] | None = None) -> RedactedInstruction:
    return RedactedInstruction(_redact_text(text, project_secrets))
