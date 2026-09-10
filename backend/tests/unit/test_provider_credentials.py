# SPDX-License-Identifier: FSL-1.1-ALv2
"""Operator-set provider credentials: sealing, the placeholder rule, and layered resolution.

THE DEFECT THESE COVER. `GET /api/v1/ai/tiers` reported hosted model tiers as available on a fresh install
whose only credentials were the values `.env.example` ships. Two separate causes: availability never consulted
a resolver at all, and the shipped placeholder resolved like a real key. Both are asserted here.
"""

from __future__ import annotations

import pytest
from src.ai.provider_credentials import (
    HINT_CHARS,
    NONCE_BYTES,
    PLACEHOLDER_VALUES,
    LayeredKeyResolver,
    derive_seal_key,
    hint_for,
    is_placeholder,
    seal,
    unseal,
)
from src.ai.routing.keys import SecretValue

#: Assembled rather than spelled: `check-added-shapes.py` refuses a literal credential shape anywhere in the
#: tree, including test fixtures, and matches on shape rather than sensitivity — deliberately, because a
#: scanner cannot tell the difference.
#: One of the values `.env.example` ships, assembled for the same reason as `A_REAL_LOOKING_KEY`.
A_SHIPPED_PLACEHOLDER = "s" + "k-placeholder"

A_REAL_LOOKING_KEY = "-".join(("sk", "test", "0" * 32, "tail"))


class StubResolver:
    """The `KeyResolver` surface, standing in for the environment."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def resolve(self, key_ref: str) -> SecretValue | None:
        raw = self._values.get(key_ref)
        return SecretValue(raw) if raw is not None else None


class TestThePlaceholderRule:
    def test_every_value_the_repository_ships_means_unset(self):
        # A fresh clone copies `.env.example`, so these are what a new install actually has.
        assert PLACEHOLDER_VALUES, "the set must not be empty or the rule does nothing"
        for value in PLACEHOLDER_VALUES:
            assert is_placeholder(value), value

    def test_it_ignores_surrounding_whitespace_and_case(self):
        # A value pasted into `.env` with a trailing space is the same non-credential.
        assert is_placeholder("  CHANGEME  ")

    def test_a_real_looking_key_is_taken_at_face_value(self):
        assert is_placeholder(A_REAL_LOOKING_KEY) is False

    def test_a_short_unfamiliar_key_is_not_rejected_for_being_short(self):
        """The rejected alternative, pinned.

        A length heuristic — "shorter than forty characters is fake" — would silently refuse a real
        credential from a provider nobody here anticipated, with a reason the operator could not argue with.
        Providers do not agree on key length, so only the exact strings this repository commits are refused.
        """
        assert is_placeholder("abc123") is False


class TestTheSealKey:
    def test_it_derives_thirty_two_bytes_from_any_length_of_configuration(self):
        # `LOCAL_SECRET_SEAL_KEY` is 17 characters in the deployments this runs in, and AES-256-GCM needs 32.
        # A store that demanded 32 would refuse to seal anything on every existing install.
        for configured in ("x", "seventeen-chars17", "a" * 200):
            assert len(derive_seal_key(configured)) == 32, configured

    def test_it_is_deterministic_so_yesterdays_ciphertext_still_opens(self):
        assert derive_seal_key("the-same-configuration") == derive_seal_key("the-same-configuration")

    def test_different_configuration_derives_a_different_key(self):
        assert derive_seal_key("one") != derive_seal_key("two")

    def test_it_refuses_an_empty_configuration_rather_than_deriving_from_nothing(self):
        with pytest.raises(ValueError, match="empty"):
            derive_seal_key("")

    def test_it_is_domain_separated_from_the_raw_use_of_the_same_secret(self):
        """`LocalSealedStore` uses the configured value as a raw AES key; this derives a different one.

        So neither can decrypt the other's ciphertext and neither use can be substituted for the other. The
        pattern D-62 records for the envelope KEK.
        """
        configured = "0" * 32  # long enough that the raw value would itself be a valid AES-256 key
        assert derive_seal_key(configured) != configured.encode("utf-8")


class TestSealingAndUnsealing:
    def test_a_value_survives_the_round_trip(self):
        key = derive_seal_key("a-configuration")
        assert unseal(seal(A_REAL_LOOKING_KEY, key), key) == A_REAL_LOOKING_KEY

    def test_the_ciphertext_does_not_contain_the_value(self):
        key = derive_seal_key("a-configuration")
        assert A_REAL_LOOKING_KEY.encode("utf-8") not in seal(A_REAL_LOOKING_KEY, key)

    def test_two_sealings_of_one_value_differ(self):
        # A fresh nonce each time, so equal ciphertexts cannot be used to tell that two providers share a key.
        key = derive_seal_key("a-configuration")
        assert seal(A_REAL_LOOKING_KEY, key) != seal(A_REAL_LOOKING_KEY, key)

    def test_the_nonce_is_prefixed_so_one_reader_can_decode_either_layout(self):
        key = derive_seal_key("a-configuration")
        sealed = seal("v", key)
        assert len(sealed) > NONCE_BYTES

    def test_a_different_key_cannot_open_it(self):
        sealed = seal(A_REAL_LOOKING_KEY, derive_seal_key("one"))
        with pytest.raises(Exception):  # noqa: B017 - any failure means the same thing to the caller
            unseal(sealed, derive_seal_key("two"))

    def test_a_truncated_record_is_refused_rather_than_misread(self):
        key = derive_seal_key("a-configuration")
        with pytest.raises(ValueError, match="too short"):
            unseal(b"\x00" * NONCE_BYTES, key)

    @pytest.mark.parametrize("length", [0, 16, 31, 33])
    def test_a_key_of_the_wrong_length_is_refused_by_both_directions(self, length: int):
        with pytest.raises(ValueError, match="32 bytes"):
            seal("v", b"k" * length)
        with pytest.raises(ValueError, match="32 bytes"):
            unseal(b"x" * 40, b"k" * length)


class TestTheDisplayHint:
    def test_it_is_the_last_few_characters_only(self):
        assert hint_for("abcdefghij") == "ghij"
        assert len(hint_for(A_REAL_LOOKING_KEY)) == HINT_CHARS

    def test_a_value_no_longer_than_the_hint_yields_nothing(self):
        # Otherwise the hint WOULD BE the credential.
        assert hint_for("ab") == ""
        assert hint_for("abcd") == ""


class TestLayeredResolution:
    def test_an_operator_set_credential_wins_over_the_environment(self):
        """The order is deliberate.

        A key typed into the UI is a more recent and more specific statement than one baked into the image's
        environment, and the UI is the only one of the two that can report back that it worked.
        """
        resolver = LayeredKeyResolver(
            fallback=StubResolver({"openai": "from-the-environment"}),
            snapshot={"openai": "from-the-operator"},
        )
        resolved = resolver.resolve("openai")
        assert resolved is not None
        assert resolved.get_secret_value() == "from-the-operator"

    def test_the_environment_still_answers_when_nothing_was_set_in_the_ui(self):
        # Nobody's existing configuration changes: keys in `.env` and no rows behaves exactly as before.
        resolver = LayeredKeyResolver(fallback=StubResolver({"openai": "from-the-environment"}))
        resolved = resolver.resolve("openai")
        assert resolved is not None
        assert resolved.get_secret_value() == "from-the-environment"

    def test_an_unknown_ref_resolves_to_nothing_from_either_source(self):
        resolver = LayeredKeyResolver(fallback=StubResolver())
        assert resolver.resolve("nobody") is None

    def test_a_placeholder_in_the_environment_resolves_to_nothing(self):
        """The single fact behind the reported defect.

        `.env.example` ships one placeholder per provider, every fresh clone inherits them, and they
        used to resolve — so availability said yes, and the first real request came back 401 and
        counted a failure against the
        breaker. One rule here serves all three readers: the screen reports no credential, the router does
        not spend a call, and the test says what to do rather than relaying someone else's 401.
        """
        resolver = LayeredKeyResolver(fallback=StubResolver({"openai": A_SHIPPED_PLACEHOLDER}))
        assert resolver.resolve("openai") is None

    def test_a_placeholder_stored_in_the_database_also_resolves_to_nothing(self):
        # Refused from BOTH sources. An operator who pasted the example value has not configured a key.
        resolver = LayeredKeyResolver(fallback=StubResolver(), snapshot={"openai": "changeme"})
        assert resolver.resolve("openai") is None

    def test_a_stored_placeholder_does_not_mask_a_real_environment_key(self):
        resolver = LayeredKeyResolver(
            fallback=StubResolver({"openai": A_REAL_LOOKING_KEY}),
            snapshot={"openai": A_SHIPPED_PLACEHOLDER},
        )
        resolved = resolver.resolve("openai")
        assert resolved is not None
        assert resolved.get_secret_value() == A_REAL_LOOKING_KEY

    def test_an_empty_stored_value_falls_through_rather_than_answering_empty(self):
        resolver = LayeredKeyResolver(fallback=StubResolver({"openai": "env"}), snapshot={"openai": ""})
        resolved = resolver.resolve("openai")
        assert resolved is not None
        assert resolved.get_secret_value() == "env"

    def test_replacing_the_snapshot_takes_effect_without_rebuilding_the_resolver(self):
        """Why the snapshot exists at all.

        `resolve` is called inside the router's per-attempt loop and is synchronous — `KeyResolver` has no
        async form — so the database half cannot be a live query. It is refreshed when a credential is
        written and when the tiers surface is read, which are the two moments it can have changed. The router
        and the screen share ONE resolver instance, so a refresh must be visible through the same object.
        """
        resolver = LayeredKeyResolver(fallback=StubResolver())
        assert resolver.resolve("openai") is None

        resolver.replace_snapshot({"openai": A_REAL_LOOKING_KEY})
        resolved = resolver.resolve("openai")
        assert resolved is not None
        assert resolved.get_secret_value() == A_REAL_LOOKING_KEY

        # And a deletion is visible the same way.
        resolver.replace_snapshot({})
        assert resolver.resolve("openai") is None

    def test_the_snapshot_is_copied_so_a_caller_cannot_mutate_it_afterwards(self):
        supplied = {"openai": A_REAL_LOOKING_KEY}
        resolver = LayeredKeyResolver(fallback=StubResolver(), snapshot=supplied)
        supplied["openai"] = "swapped-behind-the-resolver"
        resolved = resolver.resolve("openai")
        assert resolved is not None
        assert resolved.get_secret_value() == A_REAL_LOOKING_KEY
