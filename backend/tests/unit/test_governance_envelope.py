# SPDX-License-Identifier: FSL-1.1-ALv2
"""§7.6 envelope canonicalisation, signing, and the cross-runtime fixture corpus.

design.md §7.6, §11.6, Appendix A.2; Q-14; tasks.md leaf 7.4.

This file and `agent/internal/envelope/corpus_test.go` read the **same files** — the point of
§7.6's arrangement. A divergence between the two implementations fails both suites instead of
surfacing in production as a signature error that looks like tampering.

Three things are asserted here that a reader should not have to infer:

* **The corpus is not vacuous.** A floor is committed, and it is checked against the Go
  suite's own floor constant, so the two sides cannot drift to different corpora.
* **The corpus is not stale.** `scripts/gen-envelope-fixtures.py --check` is run for real, so
  the committed bytes provably come from the committed implementation.
* **The refusals agree too.** A corpus of documents that must be accepted says nothing about
  whether the two runtimes refuse the same documents — and one side reporting "malformed"
  while the other reports "signature invalid" is worse than a byte difference.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from src.governance.envelope import (
    APPROVAL_DOMAIN_PREFIX,
    CANONICAL_MEMBERS,
    ENVELOPE_DOMAIN_PREFIX,
    MAX_SAFE_INTEGER,
    CommandEnvelope,
    EnvelopeError,
    EnvelopeSchemaError,
    PolicyContextPayload,
    SigningKeyUnavailableError,
    canonical_envelope_bytes,
    decode_signature,
    encode_signature,
    envelope_digest,
    sign_envelope,
    signing_input,
    signing_key_scope,
    verify_envelope_signature,
)

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = REPO_ROOT / "agent" / "testdata" / "envelopes"
INVALID_DIR = CORPUS_DIR / "invalid"
GO_CORPUS_TEST = REPO_ROOT / "agent" / "internal" / "envelope" / "corpus_test.go"
GENERATOR = REPO_ROOT / "scripts" / "gen-envelope-fixtures.py"

#: Committed floors, raised deliberately and never lowered. `test_the_two_suites_share_one_floor`
#: below checks these against the Go constants, so "both runtimes read the same corpus" is an
#: assertion rather than a comment.
CORPUS_FLOOR = 8
INVALID_CORPUS_FLOOR = 6

#: Each `reject` token, and the substring the Python refusal must carry. A token rather than a
#: message is what keeps the two suites from being coupled to each other's prose; the substring
#: is how this side maps the token to its own error.
REJECT_SUBSTRINGS: dict[str, str] = {
    "float": "float",
    "integer-domain": "exact integer domain",
    "unknown-member": "unknown envelope members",
    "args-not-object": "args must be an object",
    "seq-not-integer": "must be an integer",
    "signature-non-canonical": "not canonical base64url",
}


def _refuse_float(text: str) -> float:
    raise AssertionError(
        f"a corpus fixture carries the non-integer number {text!r}; §7.6 forbids a float "
        f"anywhere in an envelope, so the loader refuses one rather than testing over it"
    )


def _refuse_constant(text: str) -> Any:
    raise AssertionError(f"a corpus fixture carries the JSON constant {text!r}, which RFC 8785 cannot serialise")


def _load_strict(path: Path) -> dict[str, Any]:
    """Load a valid fixture, refusing a float or a non-finite constant at the parser.

    Refusing in the parser rather than in an assertion means the "no floats" clause cannot be
    satisfied by a fixture nobody walked.
    """
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle, parse_float=_refuse_float, parse_constant=_refuse_constant)
    assert isinstance(document, dict)
    return document


def _valid_fixtures() -> list[dict[str, Any]]:
    paths = sorted(CORPUS_DIR.glob("*.json"))
    assert len(paths) >= CORPUS_FLOOR, (
        f"corpus has {len(paths)} fixtures, the committed floor is {CORPUS_FLOOR}; a glob that "
        f"matched nothing would make every assertion in this file pass over an empty list"
    )
    fixtures = [_load_strict(path) for path in paths]
    for fixture, path in zip(fixtures, paths, strict=True):
        assert fixture["fixture"] == "forgeops-envelope-fixture-v1", path
        assert fixture["why"], f"{path} has no `why`"
    return fixtures


def _invalid_fixtures() -> list[dict[str, Any]]:
    paths = sorted(INVALID_DIR.glob("*.json"))
    assert len(paths) >= INVALID_CORPUS_FLOOR, (
        f"invalid corpus has {len(paths)} fixtures, the committed floor is {INVALID_CORPUS_FLOOR}"
    )
    # Loaded permissively on purpose: several of these fixtures exist BECAUSE they carry a
    # float, so a strict loader would reject the input before the implementation could.
    fixtures = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    for fixture, path in zip(fixtures, paths, strict=True):
        assert fixture["fixture"] == "forgeops-envelope-invalid-fixture-v1", path
        assert fixture["reject"] in REJECT_SUBSTRINGS, f"{path} declares the unknown token {fixture['reject']!r}"
    return fixtures


def _ids(fixtures: list[dict[str, Any]]) -> list[str]:
    return [str(fixture["name"]) for fixture in fixtures]


VALID = _valid_fixtures()
INVALID = _invalid_fixtures()


class TestTheCorpusIsShared:
    """The corpus is only worth having if both suites provably read the same thing."""

    def test_the_two_suites_share_one_floor(self) -> None:
        source = GO_CORPUS_TEST.read_text(encoding="utf-8")
        go_valid = re.search(r"corpusFloor\s*=\s*(\d+)", source)
        go_invalid = re.search(r"invalidCorpusFloor\s*=\s*(\d+)", source)
        assert go_valid and go_invalid, "the Go suite no longer declares its corpus floors"
        assert int(go_valid.group(1)) == CORPUS_FLOOR, (
            f"Go's corpusFloor is {go_valid.group(1)} and Python's is {CORPUS_FLOOR}; the two "
            f"suites would then be asserting over different corpora"
        )
        assert int(go_invalid.group(1)) == INVALID_CORPUS_FLOOR

    def test_the_go_suite_reads_this_directory(self) -> None:
        """A path typo on either side would silently reduce one suite to a no-op."""
        source = GO_CORPUS_TEST.read_text(encoding="utf-8")
        assert 'corpusDir = "../../testdata/envelopes"' in source, (
            "the Go suite's corpus path changed; §7.6 requires both runtimes to read the same files"
        )
        assert (REPO_ROOT / "agent" / "internal" / "envelope").exists()
        assert CORPUS_DIR.is_dir()

    def test_the_corpus_is_in_sync_with_its_generator(self) -> None:
        """The committed bytes must provably come from the committed implementation.

        Without this, a hand-edited fixture could make both suites agree on a value neither
        implementation produces — a corpus that tests the corpus.
        """
        completed = subprocess.run(  # noqa: S603 - a committed script, fixed argv
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


class TestTheCorpusMatchesThisImplementation:
    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_canonical_bytes_match_the_committed_hex(self, fixture: dict[str, Any]) -> None:
        envelope = CommandEnvelope.from_mapping(fixture["envelope"])
        canonical = canonical_envelope_bytes(envelope)
        assert canonical.hex() == fixture["canonical_hex"], (
            f"canonical bytes differ from the committed corpus.\n py: {canonical!r}\n "
            f"corpus: {bytes.fromhex(fixture['canonical_hex'])!r}"
        )

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_canonical_utf8_agrees_with_the_hex(self, fixture: dict[str, Any]) -> None:
        """Editing one representation without the other would leave a fixture that looks right."""
        assert fixture["canonical_utf8"] == bytes.fromhex(fixture["canonical_hex"]).decode("utf-8")

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_signing_input_digest_matches(self, fixture: dict[str, Any]) -> None:
        envelope = CommandEnvelope.from_mapping(fixture["envelope"])
        payload = signing_input(fixture["domain_prefix"], envelope)
        assert payload.startswith(fixture["domain_prefix"].encode("utf-8") + b"\x00")
        assert sha256(payload).hexdigest() == fixture["signing_input_sha256"]

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_signature_matches_under_the_synthetic_key(self, fixture: dict[str, Any]) -> None:
        assert fixture["key_utf8"].startswith("test-only-not-a-real-secret"), (
            "the fixture key must be self-labelling as synthetic (.kiro/steering/secret-safety.md)"
        )
        envelope = CommandEnvelope.from_mapping(fixture["envelope"])
        with signing_key_scope(fixture["key_utf8"].encode("utf-8")):
            signature = sign_envelope(envelope, prefix=fixture["domain_prefix"])
        assert signature == fixture["signature"]

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_verification_accepts_only_the_committed_signature(self, fixture: dict[str, Any]) -> None:
        envelope = CommandEnvelope.from_mapping(fixture["envelope"])
        key = fixture["key_utf8"].encode("utf-8")
        assert verify_envelope_signature(envelope, fixture["signature"], key, prefix=fixture["domain_prefix"])
        # Every single-byte mutation of the MAC must be rejected. Walked over the decoded bytes
        # rather than the text, so the assertion is about the MAC and not about base64.
        mac = bytearray(decode_signature(fixture["signature"]))
        for index in range(len(mac)):
            original = mac[index]
            mac[index] = original ^ 0x01
            mutated = encode_signature(bytes(mac))
            assert not verify_envelope_signature(envelope, mutated, key, prefix=fixture["domain_prefix"]), (
                f"a MAC differing in byte {index} verified"
            )
            mac[index] = original

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_the_other_domain_prefix_never_produces_the_same_signature(self, fixture: dict[str, Any]) -> None:
        """Q-14's negative control: remove the prefix on one side only and this fails."""
        other = APPROVAL_DOMAIN_PREFIX if fixture["domain_prefix"] == ENVELOPE_DOMAIN_PREFIX else ENVELOPE_DOMAIN_PREFIX
        envelope = CommandEnvelope.from_mapping(fixture["envelope"])
        with signing_key_scope(fixture["key_utf8"].encode("utf-8")):
            wrong = sign_envelope(envelope, prefix=other)
        assert wrong != fixture["signature"]
        # The prefix separates the signing INPUT, never the document.
        assert canonical_envelope_bytes(envelope).hex() == fixture["canonical_hex"]

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_no_member_is_ever_a_float(self, fixture: dict[str, Any]) -> None:
        """The loader already refuses one; this walks the parsed document as well.

        Both, because the loader guards the file and this guards the *values* — an integer that
        arrived as `1e3` would parse as a float on some readers and not others.
        """
        _assert_no_float(fixture["envelope"], "envelope")

    @pytest.mark.parametrize("fixture", VALID, ids=_ids(VALID))
    def test_every_canonical_member_is_present(self, fixture: dict[str, Any]) -> None:
        assert set(fixture["envelope"]) == set(CANONICAL_MEMBERS)
        assert "signature" not in fixture["envelope"], (
            "the corpus stores the envelope without `signature` because that is what is signed"
        )


def _assert_no_float(value: Any, path: str) -> None:
    if isinstance(value, bool):
        return
    assert not isinstance(value, float), f"{path} is a float; §7.6 forbids one anywhere in an envelope"
    if isinstance(value, int):
        assert abs(value) <= MAX_SAFE_INTEGER, f"{path}={value} is outside RFC 8785's exact integer domain"
    elif isinstance(value, dict):
        for key, item in value.items():
            _assert_no_float(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_float(item, f"{path}[{index}]")


class TestTheTwoRuntimesRefuseTheSameDocuments:
    @pytest.mark.parametrize("fixture", INVALID, ids=_ids(INVALID))
    def test_every_invalid_fixture_is_refused_for_its_stated_reason(self, fixture: dict[str, Any]) -> None:
        expected = REJECT_SUBSTRINGS[fixture["reject"]]
        if fixture["reject"] == "signature-non-canonical":
            signature = fixture["envelope"]["signature"]
            assert signature != fixture["valid_signature"], "the fixture's signature is the canonical spelling"
            with pytest.raises(EnvelopeSchemaError, match=expected):
                decode_signature(signature)
            # And the whole verification path must refuse it rather than raising at a caller.
            body = {key: item for key, item in fixture["envelope"].items() if key != "signature"}
            envelope = CommandEnvelope.from_mapping(body)
            assert not verify_envelope_signature(envelope, signature, fixture["key_utf8"].encode("utf-8")), (
                "a non-canonical spelling of a valid MAC verified"
            )
            return

        with pytest.raises(EnvelopeSchemaError, match=expected):
            envelope = CommandEnvelope.from_mapping(fixture["envelope"])
            canonical_envelope_bytes(envelope)


class TestTheSigningKeyIsConfined:
    """§2.2.2: the control plane is the sole holder of the per-device envelope key."""

    def test_signing_without_a_key_in_scope_raises(self) -> None:
        with pytest.raises(SigningKeyUnavailableError, match="signing_key_scope"):
            sign_envelope(_sample_envelope())

    def test_the_scope_is_restored_even_when_the_body_raises(self) -> None:
        """A leaked key would let a later, unrelated mint sign with the wrong device's key."""
        with pytest.raises(RuntimeError), signing_key_scope(b"test-only-not-a-real-secret-key"):
            raise RuntimeError("boom")
        with pytest.raises(SigningKeyUnavailableError):
            sign_envelope(_sample_envelope())

    def test_a_nested_scope_restores_its_parent_rather_than_clearing(self) -> None:
        outer = b"test-only-not-a-real-secret-outer"
        inner = b"test-only-not-a-real-secret-inner"
        envelope = _sample_envelope()
        with signing_key_scope(outer):
            before = sign_envelope(envelope)
            with signing_key_scope(inner):
                assert sign_envelope(envelope) != before
            assert sign_envelope(envelope) == before

    @pytest.mark.parametrize("bad", [b"", "not-bytes", None, 0])
    def test_an_empty_or_non_bytes_key_is_refused(self, bad: object) -> None:
        with pytest.raises(EnvelopeError, match="non-empty bytes"):
            with signing_key_scope(bad):  # type: ignore[arg-type]
                pass

    def test_this_module_needs_no_banned_api_exemption(self) -> None:
        """It DEFINES the confined names rather than importing them, so it needs no waiver.

        Asserted because an exemption added here would silently unban the whole table for this
        file, and §2.2.1's per-file exemption list is the review that addition deserves.
        """
        pyproject = (REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
        assert '"src/governance/envelope.py" = ["TID251"]' not in pyproject


class TestTheSchemaIsStrict:
    def test_a_float_in_args_names_its_path(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match=r"args\.ratio is a float"):
            canonical_envelope_bytes(_sample_envelope(args={"ratio": 0.5}))

    def test_a_nested_float_names_its_path(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match=r"args\.limits\.share is a float"):
            canonical_envelope_bytes(_sample_envelope(args={"limits": {"share": 0.25}}))

    @pytest.mark.parametrize("field", ["seq", "not_after"])
    def test_an_integer_outside_the_exact_domain_is_refused(self, field: str) -> None:
        envelope = _sample_envelope(**{field: MAX_SAFE_INTEGER + 1})
        with pytest.raises(EnvelopeSchemaError, match="exact integer domain"):
            canonical_envelope_bytes(envelope)

    def test_an_args_integer_outside_the_exact_domain_is_refused(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match="exact integer domain"):
            canonical_envelope_bytes(_sample_envelope(args={"big": MAX_SAFE_INTEGER + 1}))

    def test_a_non_uuid_command_id_is_refused(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match="command_id must be a UUID"):
            canonical_envelope_bytes(_sample_envelope(command_id="not-a-uuid"))

    def test_a_missing_bundle_digest_is_refused(self) -> None:
        """ "No digest" must never be readable as "any digest" (Q-07)."""
        envelope = _sample_envelope()
        broken = CommandEnvelope(
            command_id=envelope.command_id,
            device_id=envelope.device_id,
            operation=envelope.operation,
            args=envelope.args,
            approval_id=envelope.approval_id,
            policy_context=PolicyContextPayload(bundle_digest="", decision="allow"),
            nonce=envelope.nonce,
            seq=envelope.seq,
            not_after=envelope.not_after,
        )
        with pytest.raises(EnvelopeSchemaError, match="bundle_digest is required"):
            canonical_envelope_bytes(broken)

    def test_an_unknown_version_is_refused(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match="envelope v must be"):
            canonical_envelope_bytes(_sample_envelope(v="2"))

    def test_a_non_positive_seq_is_refused(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match="seq must be a positive"):
            canonical_envelope_bytes(_sample_envelope(seq=0))

    def test_an_unknown_domain_prefix_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="unknown domain-separation prefix"):
            signing_input("forgeops-something-else-v1", _sample_envelope())

    def test_from_mapping_rejects_an_unknown_policy_context_member(self) -> None:
        body = dict(_sample_envelope().as_canonical_mapping())
        body["policy_context"] = {"bundle_digest": "sha256:" + "0" * 64, "decision": "allow", "extra": 1}
        with pytest.raises(EnvelopeSchemaError, match="unknown policy_context members"):
            CommandEnvelope.from_mapping(body)

    def test_from_mapping_round_trips_the_canonical_mapping(self) -> None:
        envelope = _sample_envelope(args={"root": "/srv/app", "entries": [{"n": 1}]})
        again = CommandEnvelope.from_mapping(envelope.as_canonical_mapping())
        assert canonical_envelope_bytes(again) == canonical_envelope_bytes(envelope)

    def test_absent_args_and_empty_args_canonicalise_identically(self) -> None:
        """One logical envelope must not have two canonical forms (§7.6 step 3)."""
        assert canonical_envelope_bytes(_sample_envelope(args={})) == canonical_envelope_bytes(_sample_envelope())


class TestSignatureEncoding:
    def test_encoding_is_unpadded(self) -> None:
        assert "=" not in encode_signature(b"\x00" * 32)

    def test_decoding_accepts_padding_but_never_emits_it(self) -> None:
        raw = bytes(range(32))
        unpadded = encode_signature(raw)
        assert decode_signature(unpadded) == raw
        assert decode_signature(unpadded + "=") == raw

    def test_decoding_refuses_non_canonical_trailing_bits(self) -> None:
        """A 32-byte MAC is 256 bits and 43 base64url characters carry 258.

        The two spare bits are ignored by every base64 decoder, so one MAC has four valid
        spellings unless the encoding is checked for canonicity. Without this,
        `test_verification_accepts_only_the_committed_signature` above would be false as
        written.
        """
        table = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        canonical = encode_signature(bytes(32))
        assert len(canonical) == 43
        variant = canonical[:-1] + table[table.index(canonical[-1]) ^ 0b01]
        assert variant != canonical
        with pytest.raises(EnvelopeSchemaError, match="not canonical base64url"):
            decode_signature(variant)

    def test_an_empty_signature_is_refused(self) -> None:
        with pytest.raises(EnvelopeSchemaError, match="signature is empty"):
            decode_signature("")


class TestEnvelopeDigest:
    def test_the_digest_is_the_sha256_of_the_signing_input(self) -> None:
        envelope = _sample_envelope()
        assert envelope_digest(envelope) == sha256(signing_input(ENVELOPE_DOMAIN_PREFIX, envelope)).hexdigest()

    def test_the_digest_changes_with_any_member(self) -> None:
        base = envelope_digest(_sample_envelope())
        assert envelope_digest(_sample_envelope(seq=2)) != base
        assert envelope_digest(_sample_envelope(operation="scan.full")) != base


def _sample_envelope(**overrides: Any) -> CommandEnvelope:
    defaults: dict[str, Any] = {
        "command_id": "0e9b1d2f-6a44-4c8e-9f31-2b7d5c1a8e40",
        "device_id": "4f3a1e4c-91f2-4a1b-8d47-6c2f0b5a9e11",
        "operation": "changeset.apply",
        "args": {},
        "approval_id": "8c1f7b30-52d9-4e6a-b1c4-9a3e0f5d7268",
        "policy_context": PolicyContextPayload(bundle_digest="sha256:" + "0" * 64, decision="allow"),
        "nonce": "9f2c4b6a8d0e1f3a5c7b9d1e3f5a7c9b",
        "seq": 1,
        "not_after": 1_767_225_600,
    }
    defaults.update(overrides)
    return CommandEnvelope(**defaults)
