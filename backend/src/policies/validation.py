import json
import os
import re
import shutil
import subprocess
import tempfile

from fastapi import HTTPException, status


def validate_rego(rego_rules: str) -> None:
    """Validate Rego code using 'opa check' or static fallback."""
    if not rego_rules.strip() or "package " not in rego_rules:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="[rego_parse_error] Invalid Rego syntax: missing package declaration",
        )

    if re.search(r"^\s*default\s+\w+\s*==", rego_rules, re.MULTILINE):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="[rego_parse_error] Invalid Rego syntax: illegal token in default rule",
        )

    opa_bin = shutil.which("opa")
    if not opa_bin:
        return

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rego", delete=False) as f:
        f.write(rego_rules)
        temp_path = f.name

    try:
        result = subprocess.run([opa_bin, "check", "-f", "json", temp_path], capture_output=True, text=True)
        if result.returncode != 0:
            output = result.stdout if result.stdout.strip() else result.stderr
            try:
                parsed = json.loads(output)
                errors = parsed.get("errors", []) if isinstance(parsed, dict) else []
                if errors and isinstance(errors, list):
                    err = errors[0]
                    message = err.get("message", "Invalid Rego syntax")
                    rule_id = err.get("code", "rego_parse_error")
                    detail = f"[{rule_id}] {message}"
                    if "location" in err:
                        line = err["location"].get("row")
                        detail += f" at line {line}"
                    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
            except json.JSONDecodeError:
                pass
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"[rego_parse_error] {output}")
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
