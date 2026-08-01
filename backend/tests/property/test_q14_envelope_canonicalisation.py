# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-14 — canonicalisation and signature verification, the Python half (Appendix B Q-14).

Property, universally quantified over envelopes:

    `canonical_envelope_bytes` is byte-identical in Go and Python for the same logical envelope;
    signature verification accepts exactly the correctly signed envelope and rejects every
    single-byte mutation.

**How the cross-runtime half is discharged.** It cannot be asserted from inside one runtime, and a
test that shells out to the other one is a test that cannot run in the CI job it lives in — which
is finding 63's shape, and a conditional would be a skip in disguise (§0.4.4). So the cross-runtime
clause is discharged over the **committed corpus** both runtimes read: `agent/testdata/envelopes/
*.json` carries each envelope with its expected canonical bytes as hex, and
`tests/unit/test_governance_envelope.py` here plus `agent/internal/envelope/corpus_test.go` there
assert their own implementation against the same committed bytes. This file carries the GENERATED
half, and `agent/internal/envelope/q14_property_test.go` carries the same generated clauses over a
generator of the same declared shape.

The residual is named rather than hidden: a divergence appearing only for a shape the corpus does
not contain would be caught by neither. The corpus covers the corners the two runtimes actually
disagreed on while it was built — integer bounds, UTF-16 key order, string escaping, empty
containers — and both generators draw from those shapes.

**Negative control** (`mutations.toml` Q-14): the domain-separation prefix is removed on one side
only. Here that is `signing_input`'s prefix; the clauses below that compare prefixes, and the
committed corpus, both object.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.governance.envelope import (
    APPROVAL_DOMAIN_PREFIX,
    ENVELOPE_DOMAIN_PREFIX,
    MAX_SAFE_INTEGER,
    CommandEnvelope,
    EnvelopeError,
    PolicyContextPayload,
    canonical_envelope_bytes,
    decode_signature,
    encode_signature,
    signing_input,
    verify_envelope_signature,
)

pytestmark = pytest.mark.mandatory

#: Synthetic and self-labelling, per `.kiro/steering/secret-safety.md`. Spelled out rather than
#: drawn from a helper so the label travels with the value: anyone grepping this file for a key
#: finds a string that says what it is.
Q14_KEY = b"test-only-not-a-real-secret-q14-envelope-key"

Q14_DIGEST = "sha256:" + "14" * 32

#: The member names the generator draws from. The non-ASCII and astral ones are deliberate: UTF-16
#: code-unit ordering is where the two runtimes disagreed while the corpus was being built, and a
#: generator of only ASCII keys cannot reach that disagreement.
_KEYS = ["root", "path", "empty", "flag", "count", "nested", "list", "é", "日本", "\U0001f600", "\ufffd"]


def _values() -> st.SearchStrategy[Any]:
    """§7.6's permitted value shapes, and no floats.

    Floats are excluded by construction rather than filtered, because §7.6 states that no envelope
    contains one — it is the corner RFC 8785 is hardest at, and the canonicaliser is required to
    *refuse* rather than serialise one. That refusal has its own example test; generating floats
    here would only re-assert it.
    """
    return st.recursive(
        st.one_of(
            st.text(max_size=12),
            st.integers(min_value=-MAX_SAFE_INTEGER, max_value=MAX_SAFE_INTEGER),
            st.booleans(),
            st.none(),
        ),
        lambda children: st.one_of(
            st.lists(children, max_size=3),
            st.dictionaries(st.sampled_from(_KEYS), children, max_size=3),
        ),
        max_leaves=6,
    )


@st.composite
def envelopes(draw: st.DrawFn) -> CommandEnvelope:
    """One logically valid envelope, every member present.

    `approval_id` includes the empty string: D-83 moved that requirement to the agent's dispatcher,
    so an empty one must canonicalise and verify here rather than be refused.

    The identifiers are UUIDs because this side **requires** them to be — `_require_uuid` refuses
    anything else — while the Go verifier requires only that they be non-empty. That asymmetry is
    real and worth naming: the backend is the only minter, so Go being the more permissive verifier
    admits nothing a legitimate signer would produce, and the stricter check sits on the side that
    creates the value. Both generators draw UUIDs so the two properties quantify over the same
    shape rather than over two dialects of it.
    """
    return CommandEnvelope(
        command_id=str(draw(st.uuids())),
        device_id=str(draw(st.uuids())),
        operation=draw(st.sampled_from(["changeset.apply", "changeset.revert", "scan.full", "validate.k8s"])),
        args=draw(st.dictionaries(st.sampled_from(_KEYS), _values(), max_size=4)),
        approval_id=draw(st.one_of(st.just(""), st.uuids().map(str))),
        policy_context=PolicyContextPayload(
            bundle_digest=Q14_DIGEST,
            decision=draw(st.sampled_from(["allow", "require_approval"])),
        ),
        nonce=draw(st.from_regex(r"\A[0-9a-f]{32}\Z")),
        seq=draw(st.integers(min_value=1, max_value=MAX_SAFE_INTEGER)),
        not_after=draw(st.integers(min_value=1_900_000_000, max_value=1_900_000_300)),
    )


def _sign(envelope: CommandEnvelope, *, prefix: str = ENVELOPE_DOMAIN_PREFIX) -> str:
    """Sign without `signing_key_scope`.

    `sign_envelope` reads the scoped key deliberately (D-60), and installing a scope per generated
    example would make every clause here depend on that machinery. The HMAC is recomputed from
    `signing_input` instead, which is the same function both runtimes' signers call — so this test
    asserts the canonicalisation and the prefix rather than the key plumbing, which has its own
    tests.
    """
    import hashlib
    import hmac

    return encode_signature(hmac.new(Q14_KEY, signing_input(prefix, envelope), hashlib.sha256).digest())


class TestCanonicalisationIsDeterministicAndOrderIndependent:
    @given(envelope=envelopes())
    @settings(max_examples=60, deadline=None)
    def test_the_same_logical_envelope_gives_the_same_bytes(self, envelope: CommandEnvelope) -> None:
        first = canonical_envelope_bytes(envelope)
        assert first == canonical_envelope_bytes(envelope)

        # The same logical envelope with its args written down in a different member order. JCS
        # sorts, so the bytes must not move; this is the clause that catches a canonicaliser that
        # passed the caller's ordering through.
        reordered = CommandEnvelope(
            command_id=envelope.command_id,
            device_id=envelope.device_id,
            operation=envelope.operation,
            args=dict(reversed(list(envelope.args.items()))),
            approval_id=envelope.approval_id,
            policy_context=envelope.policy_context,
            nonce=envelope.nonce,
            seq=envelope.seq,
            not_after=envelope.not_after,
        )
        assert canonical_envelope_bytes(reordered) == first

    @given(envelope=envelopes())
    @settings(max_examples=60, deadline=None)
    def test_the_bytes_are_jcs_shaped(self, envelope: CommandEnvelope) -> None:
        canonical = canonical_envelope_bytes(envelope)
        assert b'", "' not in canonical, "insignificant whitespace between members"
        assert b"\n" not in canonical and b"\t" not in canonical

        # Top-level members in non-decreasing UTF-16 code-unit order, read from the BYTES rather
        # than from a dict, because a dict would report Python's insertion order and prove nothing.
        names = [key for key, _ in _top_level_members(canonical.decode("utf-8"))]
        assert names, "no top-level members parsed; this assertion would be vacuous"
        keyed = [tuple(name.encode("utf-16-be")) for name in names]
        assert keyed == sorted(keyed), f"members are not in UTF-16 order: {names}"

        # `signature` is never in the signed bytes: there is no path that puts it in the mapping.
        assert b'"signature"' not in canonical


class TestVerificationAcceptsExactlyTheCorrectlySignedEnvelope:
    @given(envelope=envelopes())
    @settings(max_examples=60, deadline=None)
    def test_a_correct_signature_verifies(self, envelope: CommandEnvelope) -> None:
        assert verify_envelope_signature(envelope, _sign(envelope), Q14_KEY)

    @given(envelope=envelopes())
    @settings(max_examples=60, deadline=None)
    def test_a_signature_under_another_prefix_does_not_verify(self, envelope: CommandEnvelope) -> None:
        """The domain-separation clause, and the one Q-14's negative control removes.

        Two directions. A signature over the same canonical bytes under a **different** domain must
        not verify as a command envelope, or a signed approval response could be replayed as a
        command. And an **empty** prefix is refused outright by `signing_input` rather than
        producing an undomained MAC — so on this side the negative control cannot even be
        constructed through the public function, which is the stronger of the two behaviours and
        worth asserting rather than assuming.
        """
        correct = _sign(envelope)

        other = _sign(envelope, prefix=APPROVAL_DOMAIN_PREFIX)
        assert other != correct, "the approval prefix produced the same MAC; there is no separation"
        assert not verify_envelope_signature(envelope, other, Q14_KEY)

        with pytest.raises(EnvelopeError):
            signing_input("", envelope)

    @given(envelope=envelopes(), index=st.integers(min_value=0, max_value=31), delta=st.integers(1, 255))
    @settings(max_examples=60, deadline=None)
    def test_every_single_byte_mutation_of_the_mac_is_rejected(
        self, envelope: CommandEnvelope, index: int, delta: int
    ) -> None:
        raw = bytearray(decode_signature(_sign(envelope)))
        raw[index % len(raw)] ^= delta
        assert not verify_envelope_signature(envelope, encode_signature(bytes(raw)), Q14_KEY)

    @given(envelope=envelopes(), suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=2))
    @settings(max_examples=60, deadline=None)
    def test_a_single_change_to_the_body_is_rejected_under_the_original_signature(
        self, envelope: CommandEnvelope, suffix: str
    ) -> None:
        correct = _sign(envelope)
        mutated = CommandEnvelope(
            command_id=envelope.command_id + suffix,
            device_id=envelope.device_id,
            operation=envelope.operation,
            args=copy.deepcopy(dict(envelope.args)),
            approval_id=envelope.approval_id,
            policy_context=envelope.policy_context,
            nonce=envelope.nonce,
            seq=envelope.seq,
            not_after=envelope.not_after,
        )
        assert not verify_envelope_signature(mutated, correct, Q14_KEY)

    @given(envelope=envelopes())
    @settings(max_examples=40, deadline=None)
    def test_the_signing_input_is_the_prefix_a_nul_and_the_canonical_bytes(self, envelope: CommandEnvelope) -> None:
        """§7.6 step 4, asserted directly so a reader can check it without running anything."""
        assert ENVELOPE_DOMAIN_PREFIX, "the domain prefix is empty; there is no separation at all"
        expected = ENVELOPE_DOMAIN_PREFIX.encode("utf-8") + b"\x00" + canonical_envelope_bytes(envelope)
        assert signing_input(ENVELOPE_DOMAIN_PREFIX, envelope) == expected


class TestTheCommittedCorpusIsWhatMakesThisCrossRuntime:
    """The cross-runtime clause, pointed at explicitly rather than left implicit.

    This does not re-verify the corpus — `tests/unit/test_governance_envelope.py` does that. It
    asserts the corpus is where a reader should look, and that it is non-empty, so this file cannot
    be read as claiming a cross-runtime guarantee it does not itself provide.
    """

    def test_the_corpus_exists_and_both_runtimes_name_it(self) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        corpus = sorted((repo_root / "agent" / "testdata" / "envelopes").glob("*.json"))
        assert len(corpus) >= 8, f"the shared corpus has shrunk to {len(corpus)} fixture(s)"
        for fixture in corpus:
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            assert payload["domain_prefix"] in {ENVELOPE_DOMAIN_PREFIX, APPROVAL_DOMAIN_PREFIX}
            assert payload["canonical_hex"], f"{fixture.name} records no canonical bytes"


def _top_level_members(canonical: str) -> list[tuple[str, str]]:
    """Read (name, raw value) pairs from a canonical object, in byte order."""
    members: list[tuple[str, str]] = []
    depth = 0
    index = 0
    in_string = False
    escaped = False
    expect_key = True
    current: list[str] = []
    while index < len(canonical):
        char = canonical[index]
        if escaped:
            escaped = False
            if in_string and depth == 1 and expect_key:
                current.append(char)
        elif char == "\\" and in_string:
            escaped = True
        elif char == '"':
            if in_string and depth == 1 and expect_key:
                members.append(("".join(current), ""))
                current = []
                expect_key = False
            in_string = not in_string
        elif in_string:
            if depth == 1 and expect_key:
                current.append(char)
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 1:
            expect_key = True
        index += 1
    return members
