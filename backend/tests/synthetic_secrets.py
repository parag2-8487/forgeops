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


def pem_armour(label: str) -> str:
    """Build any PEM begin-armour line, for labels that are not `... KEY`.

    `pem_header` above hard-codes the `KEY` suffix, which covers the private-key cases FO-SEC001
    was written for. Leaf 8.2 needs `CERTIFICATE` and `CERTIFICATE REQUEST` too — a certificate is
    not a secret, but the gate matches on **shape** and not on sensitivity, deliberately: "a
    scanner cannot tell, and a blocked scan that gets waved through is worse than no scan". So the
    armour is assembled here rather than exempted there.
    """
    return "-" * 5 + "BEGIN " + label + "-" * 5


def bearer_clause() -> str:
    """An `Authorization`-style clause carrying an obviously fake token."""
    return "Bearer " + SYNTHETIC_MARKER + ".not-a-jwt"


def bearer_with(value: str) -> str:
    """`Bearer <value>`, assembled so no `Bearer `-prefixed literal exists in source.

    Added for Q-19, which has to present a *range* of malformed credentials to prove each
    one is refused. Writing them inline put `Bearer …` literals in
    `test_q19_route_coverage.py` and the pre-push diff grep fired on them — correctly. The
    scheme name is the trigger regardless of what follows it, so the scheme is joined here
    and nowhere else.
    """
    return "Bearer" + (" " + value if value else "")


def basic_clause(user: str = "forgeops-test", password: str | None = None) -> str:
    """`Basic <base64>`, built at runtime.

    Base64 inside an `Authorization: Basic` header is *exactly* the shape of real HTTP
    Basic credentials, so a literal is indistinguishable from a leak to any scanner — and to
    a reviewer. Encoding here means the source carries the plaintext, which is
    self-labelling, and the encoded form exists only in memory.
    """
    import base64

    secret = SYNTHETIC_MARKER if password is None else password
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")


def unsigned_jwt() -> str:
    """A structurally valid JWT with `alg: none`, an empty payload and no signature.

    Needed to prove a verifier rejects on its algorithm allowlist rather than decoding and
    trusting the claims. It carries no secret of any kind — the two segments are
    `{"alg":"none"}` and `{}` — but its `eyJ` prefix is the JWT-header pattern
    `.kiro/steering/secret-safety.md` lists as high-risk, and the module docstring above
    records a real GitGuardian incident for precisely this shape. So it is encoded at
    runtime and no `eyJ`-prefixed literal appears in any source file.
    """
    import base64
    import json

    def segment(payload: dict[str, str]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return segment({"alg": "none"}) + "." + segment({}) + "."


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
