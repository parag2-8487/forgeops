# SPDX-License-Identifier: FSL-1.1-ALv2
"""The shared JCS primitive (design.md §7.6, §11.9, §16.2, Appendix A.2, A.8, Q-14).

Two subsystems depend on these bytes being identical: envelope signing, where the Go
agent recomputes them independently, and the audit hash chain, where verification
recomputes them from stored rows. So the tests cover three things:

1. **RFC 8785 conformance** on the specification's own vectors — key ordering by UTF-16
   code unit, string escaping, integer form, Unicode normalisation of keys.
2. **The float refusal**, which is stricter than RFC 8785 on purpose, with the offending
   path named.
3. **The project's own corpus**: envelope-shaped and audit-shaped payloads, asserted
   stable across dict insertion order — the property that actually protects the two
   subsystems, since a Python dict preserves insertion order and two code paths building
   the same document will not build it in the same order.
"""

from __future__ import annotations

import json

import pytest
from src.core.canonical import (
    CanonicalisationError,
    canonical_bytes,
    canonical_hash,
    canonical_json,
)

pytestmark = pytest.mark.mandatory


class TestRfc8785Conformance:
    """Vectors from RFC 8785 and its reference test suite."""

    def test_keys_are_sorted_by_utf16_code_unit_not_by_codepoint(self) -> None:
        """The subtle one, and the reason a hand-rolled sort is not acceptable.

        JCS orders members by their UTF-16 code units. For characters outside the BMP
        that differs from ordering by Unicode code point, because a surrogate pair's
        first unit (0xD800-0xDBFF) sorts below characters such as U+FB00. A naive
        `sorted(keys)` in Python sorts by code point and gets this wrong.
        """
        payload = {"\U0001f600": 1, "\ufb00": 2}
        rendered = canonical_json(payload)
        assert rendered.index("\U0001f600") < rendered.index("\ufb00"), rendered

    def test_object_keys_are_sorted(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_nested_objects_are_sorted_too(self) -> None:
        assert canonical_json({"z": {"b": 1, "a": 2}}) == '{"z":{"a":2,"b":1}}'

    def test_array_order_is_preserved(self) -> None:
        """Arrays are ordered data; sorting them would change meaning."""
        assert canonical_json([3, 1, 2]) == "[3,1,2]"

    def test_no_insignificant_whitespace(self) -> None:
        assert canonical_json({"a": [1, 2], "b": {"c": 3}}) == '{"a":[1,2],"b":{"c":3}}'

    def test_output_is_utf8_bytes(self) -> None:
        assert canonical_bytes({"k": "v"}) == b'{"k":"v"}'

    def test_non_ascii_is_emitted_literally_not_escaped(self) -> None:
        """JCS emits the character, unlike json.dumps' default ensure_ascii."""
        assert canonical_bytes({"k": "é"}) == '{"k":"é"}'.encode()

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("\u0008", r"\b"),
            ("\u0009", r"\t"),
            ("\u000a", r"\n"),
            ("\u000c", r"\f"),
            ("\u000d", r"\r"),
            ('"', r"\""),
            ("\\", r"\\"),
            ("\u0000", r"\u0000"),
            ("\u001f", r"\u001f"),
        ],
    )
    def test_control_characters_use_the_prescribed_escapes(self, raw: str, expected: str) -> None:
        assert canonical_json({"k": raw}) == '{"k":"' + expected + '"}'

    def test_integers_carry_no_decimal_point(self) -> None:
        assert canonical_json({"n": 1}) == '{"n":1}'
        assert canonical_json({"n": -0}) == '{"n":0}'

    def test_integers_up_to_the_safe_double_range_are_exact(self) -> None:
        safe = 2**53 - 1
        assert canonical_json({"n": safe}) == f'{{"n":{safe}}}'

    def test_an_integer_beyond_2_to_the_53_is_refused(self) -> None:
        """A real constraint of JCS, discovered rather than assumed.

        RFC 8785 serialises numbers as IEEE 754 doubles, so an integer above 2**53 has
        no canonical form — it cannot be represented exactly. `rfc8785` refuses it, and
        that refusal is correct: silently rounding would make two payloads that differ
        hash identically, which for the audit chain means two different actions sharing
        a hash.

        Practical consequence for §11.9: `audit_events.seq` is a BIGSERIAL, so it could
        in principle exceed this. At 9.0e15 rows that is not a real bound, but the
        canonical payload should carry semantic fields rather than the sequence anyway,
        which is what Appendix A.8 specifies.
        """
        with pytest.raises(CanonicalisationError, match="not canonicalisable"):
            canonical_bytes({"n": 2**53 + 1})

    def test_literals(self) -> None:
        assert canonical_json({"t": True, "f": False, "n": None}) == '{"f":false,"n":null,"t":true}'

    def test_the_output_is_valid_json_that_round_trips(self) -> None:
        payload = {"a": [1, {"b": "x"}], "c": None}
        assert json.loads(canonical_json(payload)) == payload


class TestFloatsAreRefused:
    """Stricter than RFC 8785, deliberately (§7.6)."""

    def test_a_top_level_float_is_refused(self) -> None:
        with pytest.raises(CanonicalisationError, match="float values are not permitted"):
            canonical_bytes(1.5)

    def test_a_nested_float_is_refused_and_its_path_named(self) -> None:
        with pytest.raises(CanonicalisationError, match=r"\$\.outer\.inner"):
            canonical_bytes({"outer": {"inner": 0.1}})

    def test_a_float_inside_an_array_is_refused_and_indexed(self) -> None:
        with pytest.raises(CanonicalisationError, match=r"\$\.scores\[2\]"):
            canonical_bytes({"scores": [1, 2, 3.5]})

    def test_a_whole_valued_float_is_still_refused(self) -> None:
        """`3.0` is the dangerous case: it looks safe and serialises differently.

        Accepting it would mean a payload whose hash depends on whether an integer
        arrived as `3` or as the result of `6 / 2`.
        """
        with pytest.raises(CanonicalisationError):
            canonical_bytes({"n": 3.0})

    def test_booleans_are_not_mistaken_for_numbers(self) -> None:
        """`bool` subclasses `int`, so the check order matters."""
        assert canonical_json({"b": True}) == '{"b":true}'

    def test_integers_are_unaffected(self) -> None:
        assert canonical_json({"n": 3}) == '{"n":3}'

    def test_a_decimal_quantity_may_be_carried_as_a_string(self) -> None:
        """The escape hatch the error message recommends, exercised."""
        assert canonical_json({"usd": "1.25"}) == '{"usd":"1.25"}'

    def test_a_non_string_key_is_refused(self) -> None:
        with pytest.raises(CanonicalisationError, match="object keys must be strings"):
            canonical_bytes({1: "a"})


class TestTheProjectCorpus:
    """Envelope-shaped and audit-shaped payloads (Appendix A.2, A.8)."""

    ENVELOPE = {
        "v": 1,
        "id": "01JBQ8Z0000000000000000000",
        "seq": 42,
        "not_after": "2026-07-30T12:00:00Z",
        "nonce": "1f9c2d3e4b5a6978",
        "operation": "files.apply",
        "approval_id": "01JBQ8Z1111111111111111111",
        "policy_context": {"bundle_digest": "sha256:" + "ab" * 32, "package": "forgeops/governance"},
        "payload": {"change_set_id": "01JBQ8Z2222222222222222222", "items": 3},
    }

    AUDIT = {
        "actor_kind": "agent",
        "actor_id": "01JBQ8Z3333333333333333333",
        "action": "change_set.apply",
        "project_id": "01JBQ8Z4444444444444444444",
        "reason": "approved by a maintainer",
        "occurred_at": "2026-07-30T12:00:01Z",
        "before": {"status": "approved"},
        "after": {"status": "applied"},
    }

    def test_canonical_form_is_independent_of_insertion_order(self) -> None:
        """The property that actually protects both subsystems.

        A Python dict preserves insertion order, and two code paths building the same
        logical document will not build it in the same order. If canonicalisation were
        order-sensitive, the audit chain would break the first time a field moved in the
        source.
        """
        for payload in (self.ENVELOPE, self.AUDIT):
            shuffled = dict(reversed(list(payload.items())))
            assert canonical_bytes(shuffled) == canonical_bytes(payload)

    def test_nested_insertion_order_is_also_irrelevant(self) -> None:
        original = self.ENVELOPE
        rearranged = {
            **original,
            "policy_context": dict(reversed(list(original["policy_context"].items()))),
        }
        assert canonical_bytes(rearranged) == canonical_bytes(original)

    def test_canonicalisation_is_stable_across_calls(self) -> None:
        assert canonical_bytes(self.ENVELOPE) == canonical_bytes(self.ENVELOPE)

    def test_removing_the_signature_field_is_the_callers_job_not_ours(self) -> None:
        """This module canonicalises what it is given; §7.6 says the signer excludes
        `signature`. Asserted so the responsibility stays where the design puts it."""
        signed = {**self.ENVELOPE, "signature": "deadbeef"}
        assert canonical_bytes(signed) != canonical_bytes(self.ENVELOPE)

    def test_a_single_byte_change_changes_the_bytes(self) -> None:
        """Q-14's rejection clause depends on this being true of every field."""
        for key in self.ENVELOPE:
            mutated = dict(self.ENVELOPE)
            value = mutated[key]
            mutated[key] = value + 1 if isinstance(value, int) and not isinstance(value, bool) else f"{value}x"
            assert canonical_bytes(mutated) != canonical_bytes(self.ENVELOPE), key


class TestDomainSeparatedHashing:
    def test_the_prefix_changes_the_hash(self) -> None:
        payload = {"a": 1}
        assert canonical_hash(payload, prefix=b"forgeops-envelope-v1\x00") != canonical_hash(
            payload, prefix=b"forgeops-approval-v1\x00"
        )

    def test_no_prefix_is_the_bare_digest(self) -> None:
        import hashlib

        payload = {"a": 1}
        assert canonical_hash(payload) == hashlib.sha256(canonical_bytes(payload)).digest()

    def test_the_prefix_is_prepended_not_appended(self) -> None:
        """Fixing the concatenation order in one place is the point of the helper.

        Two subsystems that hashed `payload || prefix` and `prefix || payload` would
        both be self-consistent and mutually incompatible.
        """
        import hashlib

        payload = {"a": 1}
        prefix = b"P\x00"
        assert canonical_hash(payload, prefix=prefix) == hashlib.sha256(prefix + canonical_bytes(payload)).digest()

    def test_a_float_is_refused_before_hashing(self) -> None:
        with pytest.raises(CanonicalisationError):
            canonical_hash({"n": 1.5})

    def test_the_digest_is_32_bytes(self) -> None:
        assert len(canonical_hash({"a": 1})) == 32


class TestOneImplementationForTwoSubsystems:
    def test_canonical_json_and_canonical_bytes_agree(self) -> None:
        payload = {"b": 1, "a": "é"}
        assert canonical_json(payload).encode("utf-8") == canonical_bytes(payload)

    def test_no_other_module_canonicalises_for_itself(self) -> None:
        """A second canonicaliser is the failure this module exists to prevent.

        Scans `src/**` for a direct `rfc8785` import: only the wrapper may have one, so
        the float rule and the byte format cannot be bypassed by a subsystem that
        reaches for the library itself.
        """
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src"
        importers: set[str] = set()
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module.split(".")[0]]
                if "rfc8785" in names:
                    importers.add(path.relative_to(src.parent).as_posix())

        assert importers == {"src/core/canonical.py"}, sorted(importers)
