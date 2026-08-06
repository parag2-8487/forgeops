#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generate `agent/testdata/envelopes/**` — the cross-runtime envelope fixture corpus.

design.md §7.6, §10.4, Appendix A.2; Q-14; tasks.md leaf 7.4.

What the corpus is for
----------------------
`backend/src/governance/envelope.py` and `agent/internal/envelope` are two implementations of
one byte-level contract. §7.6 makes them read the **same files**, so a divergence fails both
suites instead of surfacing in production as a signature error nobody can explain.

Each fixture carries the envelope plus four derived values: the canonical bytes as hex, the
same bytes as UTF-8 text, the SHA-256 of the domain-separated signing input, and the
signature. Both runtimes recompute all four from the committed envelope and compare.

Why a generator, and why regenerating is not a way to fix a test
---------------------------------------------------------------
The expected values must come from somewhere, and hand-computing an HMAC is not reviewable.
This script computes them with the Python implementation; the Go suite then verifies them
independently. That asymmetry is what makes the corpus a two-way lock:

* break Python, and Python's own test fails against the committed bytes;
* break Go, and Go's test fails against the committed bytes;
* regenerate after breaking Python, and Go fails — which is the case this whole arrangement
  exists to catch.

So **regenerating is a deliberate change to the contract**, not a repair. If a run of this
script changes any existing fixture, the correct next step is to run `go test
./internal/envelope/...` and explain the change, not to commit it quietly.

Usage
-----
    backend/.venv/Scripts/python.exe scripts/gen-envelope-fixtures.py [--check]

`--check` regenerates into memory and exits non-zero if any committed file would change, which
is how CI can assert the corpus is in sync with the generator without writing to the tree.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from src.governance.envelope import (  # noqa: E402 - sys.path must be set before this import
    APPROVAL_DOMAIN_PREFIX,
    ENVELOPE_DOMAIN_PREFIX,
    MAX_SAFE_INTEGER,
    CommandEnvelope,
    PolicyContextPayload,
    canonical_envelope_bytes,
    sign_envelope,
    signing_input,
    signing_key_scope,
)

CORPUS_DIR = REPO_ROOT / "agent" / "testdata" / "envelopes"
INVALID_DIR = CORPUS_DIR / "invalid"

#: Synthetic and self-labelling, per `.antigravity/steering/secret-safety.md`. The same literal the Go
#: suite already uses for `testKey`, so a reader comparing the two sees one value rather than
#: two that happen to match.
TEST_KEY_UTF8 = "test-only-not-a-real-secret-envelope-key"

#: Fixed identifiers. Random UUIDs would make every regeneration a diff in every file, which
#: hides the one change that matters.
DEVICE_ID = "4f3a1e4c-91f2-4a1b-8d47-6c2f0b5a9e11"
BUNDLE_DIGEST = "sha256:" + "ab" * 32
NOT_AFTER = 1_767_225_600  # 2026-01-01T00:00:00Z, an integer, never a float


def _envelope(
    *,
    command_id: str,
    operation: str,
    args: Mapping[str, Any],
    approval_id: str,
    nonce: str,
    seq: int,
    not_after: int = NOT_AFTER,
    decision: str = "allow",
) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=command_id,
        device_id=DEVICE_ID,
        operation=operation,
        args=args,
        approval_id=approval_id,
        policy_context=PolicyContextPayload(bundle_digest=BUNDLE_DIGEST, decision=decision),
        nonce=nonce,
        seq=seq,
        not_after=not_after,
    )


def _valid_cases() -> list[dict[str, Any]]:
    """The corpus, one entry per behaviour worth locking down.

    Every case states what it is *for*. A fixture whose purpose is not written down becomes a
    fixture nobody dares change and nobody can explain — Pattern F in the journal's chapter 9.
    """
    return [
        {
            "name": "01-changeset-apply-minimal",
            "why": (
                "The ordinary case. Members appear in JCS order rather than §7.6's listing "
                "order, and `args` is an empty object rather than null, because §7.7's "
                "operations all take an object and two spellings would give one logical "
                "envelope two canonical forms."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="0e9b1d2f-6a44-4c8e-9f31-2b7d5c1a8e40",
                operation="changeset.apply",
                args={},
                approval_id="8c1f7b30-52d9-4e6a-b1c4-9a3e0f5d7268",
                nonce="9f2c4b6a8d0e1f3a5c7b9d1e3f5a7c9b",
                seq=1,
            ),
        },
        {
            "name": "02-changeset-apply-nested-args",
            "why": (
                "Nested objects, an array of objects, integers, booleans and null. Proves the "
                "canonicaliser recurses and sorts at every level, not only at the top."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d",
                operation="changeset.apply",
                args={
                    "root": "/srv/app",
                    "dry_run": False,
                    "entries": [
                        {
                            "rel_path": "Dockerfile",
                            "action": "update",
                            "expected_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                            "mode": 420,
                        },
                        {
                            "rel_path": "deploy/k8s/deployment.yaml",
                            "action": "create",
                            "expected_hash": None,
                            "mode": 420,
                        },
                    ],
                    "limits": {"max_bytes": 1048576, "max_entries": 64},
                },
                approval_id="7d6c5b4a-3e2f-4a1b-8c9d-0e1f2a3b4c5d",
                nonce="11223344556677889900112233445566",
                seq=2,
            ),
        },
        {
            "name": "03-string-escaping",
            "why": (
                "RFC 8785 §3.2.2.2 minimal escaping. The two mandatory escapes, the five short "
                "forms, \\u00XX for the remaining control characters, and non-ASCII left ALONE "
                "— Go's encoding/json would escape U+2028, U+2029, <, > and &, none of which "
                "any other JCS implementation agrees with."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="2b3c4d5e-6f70-4a8b-9c0d-1e2f3a4b5c6e",
                operation="changeset.apply",
                args={
                    "quote": 'a "quoted" value',
                    "backslash": "C:\\Program Files\\app",
                    "shorthands": "\b\f\n\r\t",
                    "control": "unit\u0001separator\u001f",
                    "latin": "café résumé",
                    "cjk": "配置ファイル",
                    "emoji": "🔒 locked",
                    "html_like": "<tag> & 'apos' \u2028line \u2029para",
                },
                approval_id="6c5b4a39-2e1f-4a0b-8c9d-0e1f2a3b4c5e",
                nonce="aabbccddeeff00112233445566778899",
                seq=3,
            ),
        },
        {
            "name": "04-utf16-key-order",
            "why": (
                "RFC 8785 sorts object members by UTF-16 code unit, not by code point. A "
                "supplementary character encodes as a surrogate pair beginning 0xD800–0xDBFF, "
                "which sorts BELOW U+E000–U+FFFF in UTF-16 and ABOVE it in UTF-8. So U+1D11E "
                "must precede U+E000 here. D-59 found this; this fixture is what keeps both "
                "runtimes honest about it."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="3c4d5e6f-7081-4a8b-9c0d-1e2f3a4b5c6f",
                operation="changeset.apply",
                args={
                    "a": 1,
                    "z": 2,
                    "\ue000": "private use U+E000, one UTF-16 code unit 0xE000",
                    "\U0001d11e": "treble clef U+1D11E, surrogate pair 0xD834 0xDD1E",
                    "\uffff": "the last BMP code unit",
                    "\U0010ffff": "the last code point, surrogate pair 0xDBFF 0xDFFF",
                },
                approval_id="5b4a3928-1e0f-4a9b-8c0d-1e2f3a4b5c60",
                nonce="00112233445566778899aabbccddeeff",
                seq=4,
            ),
        },
        {
            "name": "05-integer-bounds",
            "why": (
                "Integers at RFC 8785's exact domain boundary, ±(2**53 - 1). Above it the "
                "scheme cannot round-trip a value, `rfc8785` raises, and a verbatim-decimal "
                "serialiser silently succeeds — so the boundary is asserted from both sides "
                "rather than assumed."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="4d5e6f70-8192-4a8b-9c0d-1e2f3a4b5c61",
                operation="changeset.apply",
                args={
                    "max_safe": MAX_SAFE_INTEGER,
                    "min_safe": -MAX_SAFE_INTEGER,
                    "zero": 0,
                    "one_below_max": MAX_SAFE_INTEGER - 1,
                    "no_leading_zeros": 1000000,
                },
                approval_id="4a392817-0e1f-4a8b-9c0d-1e2f3a4b5c62",
                nonce="ffeeddccbbaa99887766554433221100",
                seq=MAX_SAFE_INTEGER,
            ),
        },
        {
            "name": "06-approval-prefix-domain-separation",
            "why": (
                "The SAME envelope under the `forgeops-approval-v1` prefix. Canonical bytes are "
                "identical to what the command prefix would produce; the signing input and the "
                "signature are not. That is the whole of domain separation, and it is Q-14's "
                "negative control — remove the prefix on one side only and this fixture is the "
                "one that fails."
            ),
            "prefix": APPROVAL_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="5e6f7081-92a3-4a8b-9c0d-1e2f3a4b5c63",
                operation="changeset.apply",
                args={"approved": True, "comment": "looks right"},
                approval_id="392817f0-1e2f-4a8b-9c0d-1e2f3a4b5c64",
                nonce="0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                seq=6,
            ),
        },
        {
            "name": "07-changeset-revert",
            "why": (
                "A revert is its own mutation with its own authority (§11.6), so it is its own "
                "envelope with its own approval_id — not a reuse of the apply that preceded it."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="6f708192-a3b4-4a8b-9c0d-1e2f3a4b5c65",
                operation="changeset.revert",
                args={
                    "rollback_handle_id": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
                    "manifest": {
                        "created_at": "2026-01-01T00:00:00Z",
                        "entries": [
                            {"rel_path": "Dockerfile", "backup": "Dockerfile.forgeops-backup.1767225600"},
                            {"rel_path": "deploy/k8s/deployment.yaml", "backup": None},
                        ],
                    },
                },
                approval_id="2817f01e-2f3a-4a8b-9c0d-1e2f3a4b5c66",
                nonce="1f2e3d4c5b6a798071829384a5b6c7d8",
                seq=7,
            ),
        },
        {
            "name": "08-non-mutating-no-approval",
            "why": (
                "§7.7's read-only operations carry no approval_id. The member is still PRESENT "
                "and empty rather than absent, because canonicalisation is defined over the "
                "member set: an omitted member and an empty one produce different bytes."
            ),
            "prefix": ENVELOPE_DOMAIN_PREFIX,
            "envelope": _envelope(
                command_id="708192a3-b4c5-4a8b-9c0d-1e2f3a4b5c67",
                operation="scan.full",
                args={
                    "root": "/srv/app",
                    "empty_string": "",
                    "empty_object": {},
                    "empty_array": [],
                    "explicit_null": None,
                },
                approval_id="",
                nonce="2e3d4c5b6a798071829384a5b6c7d8e9",
                seq=8,
                decision="allow",
            ),
        },
    ]


def _render_valid(case: dict[str, Any], key: bytes) -> dict[str, Any]:
    envelope: CommandEnvelope = case["envelope"]
    prefix: str = case["prefix"]
    canonical = canonical_envelope_bytes(envelope)
    payload = signing_input(prefix, envelope)
    with signing_key_scope(key):
        signature = sign_envelope(envelope, prefix=prefix)
    wire = dict(envelope.as_canonical_mapping())
    return {
        "fixture": "forgeops-envelope-fixture-v1",
        "name": case["name"],
        "why": case["why"],
        "key_utf8": TEST_KEY_UTF8,
        "domain_prefix": prefix,
        "envelope": wire,
        "canonical_hex": canonical.hex(),
        "canonical_utf8": canonical.decode("utf-8"),
        "signing_input_sha256": sha256(payload).hexdigest(),
        "signature": signature,
    }


#: The base64url alphabet, written out because the last character of a 43-character MAC
#: encoding has to be manipulated by index below.
_B64URL_TABLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _non_canonical_signature(signature: str) -> str:
    """Return a DIFFERENT 43-character spelling of the same 32-byte MAC.

    A 32-byte MAC is 256 bits and 43 base64url characters carry 258, so the final character has
    **two** bits that decode to nothing. Setting either leaves the decoded bytes identical and
    the text different, which is why a decoder that ignores them gives one MAC four valid
    spellings. Both runtimes reject the other three by re-encoding and comparing.
    """
    index = _B64URL_TABLE.index(signature[-1])
    flipped = index ^ 0b01
    return signature[:-1] + _B64URL_TABLE[flipped]


def _invalid_cases(key: bytes) -> list[dict[str, Any]]:
    """Documents both runtimes must REFUSE, each with the reason it must be refused for.

    `reject` is a token, not a message. Asserting on a message would couple two test suites to
    English prose; asserting on a token lets each side map it to its own typed error.
    """
    good = _envelope(
        command_id="8192a3b4-c5d6-4a8b-9c0d-1e2f3a4b5c68",
        operation="changeset.apply",
        args={"root": "/srv/app"},
        approval_id="17f01e2f-3a4b-4a8b-9c0d-1e2f3a4b5c69",
        nonce="3d4c5b6a798071829384a5b6c7d8e9fa",
        seq=9,
    )
    with signing_key_scope(key):
        good_signature = sign_envelope(good)
    base = dict(good.as_canonical_mapping())

    float_args = dict(base, args={"root": "/srv/app", "ratio": 0.5})
    beyond = dict(base, seq=MAX_SAFE_INTEGER + 1)
    unknown = dict(base, extra_member="not in §7.6's member set")
    args_array = dict(base, args=["not", "an", "object"])
    float_seq = dict(base, seq=9.0)

    return [
        {
            "name": "float-in-args",
            "why": "§7.6 forbids a float anywhere in an envelope; the path must be named.",
            "reject": "float",
            "envelope": float_args,
        },
        {
            "name": "float-seq",
            "why": (
                "`seq` is an integer. A float that happens to be integral is still a float, and "
                "both runtimes must refuse it at the parse step rather than coercing it."
            ),
            "reject": "seq-not-integer",
            "envelope": float_seq,
        },
        {
            "name": "seq-beyond-safe-integer",
            "why": (
                "2**53 is outside RFC 8785's exact integer domain. Without an explicit bound "
                "the Python side raises and the Go side succeeds, so the two runtimes disagree "
                "about whether the document is canonicalisable at all."
            ),
            "reject": "integer-domain",
            "envelope": beyond,
        },
        {
            "name": "unknown-member",
            "why": "Appendix A.2's ParseStrict: an unknown member is a field one side signs and the other ignores.",
            "reject": "unknown-member",
            "envelope": unknown,
        },
        {
            "name": "args-not-an-object",
            "why": "§7.7's operations all take an object; an array would canonicalise but mean nothing.",
            "reject": "args-not-object",
            "envelope": args_array,
        },
        {
            "name": "non-canonical-signature",
            "why": (
                "A second spelling of a valid MAC. The envelope is correctly signed; the "
                "signature's two ignorable trailing bits are set, so a decoder that ignores "
                "them would accept four different strings as one signature."
            ),
            "reject": "signature-non-canonical",
            "envelope": dict(base, signature=_non_canonical_signature(good_signature)),
            "valid_signature": good_signature,
        },
    ]


def _render_invalid(case: dict[str, Any]) -> dict[str, Any]:
    out = {
        "fixture": "forgeops-envelope-invalid-fixture-v1",
        "name": case["name"],
        "why": case["why"],
        "key_utf8": TEST_KEY_UTF8,
        "domain_prefix": ENVELOPE_DOMAIN_PREFIX,
        "reject": case["reject"],
        "envelope": case["envelope"],
    }
    if "valid_signature" in case:
        out["valid_signature"] = case["valid_signature"]
    return out


def _serialise(document: Mapping[str, Any]) -> str:
    """Pretty JSON with a trailing newline, non-ASCII preserved.

    `ensure_ascii=False` is deliberate: fixture 03 and 04 exist to pin non-ASCII handling, and
    a file full of `\\uXXXX` escapes would hide what is being tested from a reviewer. Both
    runtimes read UTF-8, and `.gitattributes` keeps these files from being line-ending
    rewritten.
    """
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any committed fixture would change")
    args = parser.parse_args()

    key = TEST_KEY_UTF8.encode("utf-8")
    planned: dict[Path, str] = {}
    for case in _valid_cases():
        planned[CORPUS_DIR / f"{case['name']}.json"] = _serialise(_render_valid(case, key))
    for case in _invalid_cases(key):
        planned[INVALID_DIR / f"{case['name']}.json"] = _serialise(_render_invalid(case))

    if args.check:
        drift = [
            path.relative_to(REPO_ROOT).as_posix()
            for path, text in planned.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        if drift:
            print("envelope fixture corpus is out of sync with the generator:", file=sys.stderr)
            for name in drift:
                print(f"  {name}", file=sys.stderr)
            print(
                "Regenerating is a change to the signed contract. Run the generator, then run "
                "`go test ./internal/envelope/...` and explain the change.",
                file=sys.stderr,
            )
            return 1
        print(f"envelope fixture corpus is in sync ({len(planned)} files)")
        return 0

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    INVALID_DIR.mkdir(parents=True, exist_ok=True)
    for path, text in planned.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
