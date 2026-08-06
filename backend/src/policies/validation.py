import subprocess
import json
import tempfile
import os
from typing import Optional
from fastapi import HTTPException, status

def validate_rego(rego_rules: str) -> None:
    """Validate Rego code using 'opa check'."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".rego", delete=False) as f:
        f.write(rego_rules)
        temp_path = f.name
    
    try:
        # Check syntax and semantics
        result = subprocess.run(
            ["opa", "check", "-f", "json", temp_path],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            try:
                output = result.stdout if result.stdout.strip() else result.stderr
                parsed = json.loads(output)
                errors = parsed.get("errors", []) if isinstance(parsed, dict) else []
                if errors and isinstance(errors, list):
                    err = errors[0]
                    message = err.get("message", "Invalid Rego syntax")
                    rule_id = err.get("code", "rego_compile_error")
                    detail = f"[{rule_id}] {message}"
                    if "location" in err:
                        line = err["location"].get("row")
                        detail += f" at line {line}"
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=detail
                    )
            except json.JSONDecodeError:
                pass
            
            # Temporary debugging: include full stdout/stderr
            debug_info = f"returncode: {result.returncode}, stdout: {result.stdout}, stderr: {result.stderr}"
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=debug_info
            )
    finally:
        os.unlink(temp_path)
