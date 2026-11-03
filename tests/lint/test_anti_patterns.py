"""Anti-pattern regression linter for ForgeOps.

Verifies that no hardcoded application directory names, framework-specific heuristics,
or arbitrary retry loops exist in the codebase.
"""

import os
import re
from pathlib import Path
import pytest

PROHIBITED_DIRECTORY_STRINGS = [
    r'\[\]string\{.*"Frontent".*\}',
    r'\[\]string\{.*"frontend".*\}',
    r'\[\]string\{.*"client".*\}',
    r'\[\]string\{.*"ui".*\}',
    r'for attempt := 1; attempt <= 10',
    r'maxAttempts := 10',
]

EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "tests",
    "docs",
    ".next",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".venv",
}


def test_anti_pattern_codebase_linter():
    """Asserts that no hardcoded heuristics or blind 10-attempt loops exist in production code."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    violations = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for f in filenames:
            if not (f.endswith(".go") or f.endswith(".py")):
                continue

            file_path = Path(dirpath) / f
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            for pattern in PROHIBITED_DIRECTORY_STRINGS:
                matches = re.findall(pattern, content)
                if matches:
                    violations.append(
                        f"Prohibited pattern '{pattern}' matched in {file_path.relative_to(repo_root)}: {matches}"
                    )

    assert not violations, f"Anti-pattern regression linter found violations:\n" + "\n".join(violations)
