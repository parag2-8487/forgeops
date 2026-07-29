# SPDX-License-Identifier: FSL-1.1-ALv2
"""Synthetic secret-shaped strings for redaction tests, assembled at runtime.

Why these are not written as literals
-------------------------------------
The redaction tests have to feed the redactor something that matches its
patterns, which means the test inputs necessarily look like credentials. Writing
them as source literals makes secret scanners fire on the repository itself:

* GitGuardian incident 35267706 was a JWT-shaped placeholder in
  `test_p09_rfc9457.py` — synthetic, never a working credential, but a red gate on
  the pull request for the lifetime of that commit.
* `gitleaks detect --no-git` flags a bare PEM `BEGIN ... KEY` delimiter under its
  `private-key` rule even with no key bytes after it. (Deliberately not spelled out
  here either — a comment explaining the problem should not reproduce it.)

A blocked scan that everyone learns to wave through is worse than no scan, so the
shapes are composed from fragments here. No contiguous literal exists in any
source file, the scanners stay quiet on real content, and the assertions are
unchanged in strength: `redact_secrets` still receives the exact string it must
match.

Every value is self-labelling per `.kiro/steering/secret-safety.md` and none has
ever been a usable credential.
"""

from __future__ import annotations

# The self-labelling marker every synthetic value carries, so anything that leaks
# into output is instantly identifiable as test material.
SYNTHETIC_MARKER = "test-only-not-a-real-secret"


def pem_header(kind: str = "RSA PRIVATE") -> str:
    """Build a PEM delimiter without a contiguous literal in the source."""
    return "-" * 5 + "BEGIN " + kind + " " + "KEY" + "-" * 5


def bearer_clause() -> str:
    """An `Authorization`-style clause carrying an obviously fake token."""
    return "Bearer " + SYNTHETIC_MARKER + ".not-a-jwt"


def postgres_dsn() -> str:
    """A PostgreSQL DSN whose password is the synthetic marker."""
    return "postgresql+asyncpg://forgeops:" + SYNTHETIC_MARKER + "@db:5432/prod"


def redis_dsn() -> str:
    """A Redis DSN whose password is the synthetic marker."""
    return "redis://:" + SYNTHETIC_MARKER + "@cache:6379/0"


def openai_style_key() -> str:
    """An `sk-`-prefixed key long enough to match the redaction pattern."""
    return "sk-" + "testonlynotarealsecret0000"


def anthropic_style_key() -> str:
    """An `sk-ant-`-prefixed key long enough to match the redaction pattern."""
    return "sk-ant-" + "testonlynotarealsecret0000"
