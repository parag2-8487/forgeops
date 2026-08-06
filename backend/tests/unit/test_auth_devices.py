# SPDX-License-Identifier: FSL-1.1-ALv2
"""Envelope-key custody (design.md §11.2, §2.2.2, §17.1 D-62; leaf 7.5).

What these tests are for
------------------------
D-62 makes four claims, and each one is only worth making if it is falsifiable:

1. the key-encryption key is **derived** from `ENVELOPE_PEPPER` and from nothing else, under a
   label that domain-separates it from the pepper's HMAC use;
2. the AEAD is **bound to the row**, so a ciphertext lifted from one `agent_devices` row cannot
   be unsealed under another;
3. every seal uses a **fresh** 96-bit nonce, asserted across a generated set rather than trusted;
4. an unseal failure is **indistinguishable** whatever caused it, so a database-level attacker
   gets no oracle.

Claim 2 is the one the addition to D-62 exists for. Without the device id as additional
authenticated data, an attacker with `UPDATE` on `agent_devices` could transplant a ciphertext
whose plaintext they know onto a victim's row and then sign envelopes that device would accept —
without ever learning the victim's key. `test_a_ciphertext_does_not_unseal_under_another_device`
is the executable form of that argument.
"""

from __future__ import annotations

import uuid

import pytest
from src.auth.devices import (
    ENVELOPE_KEY_BYTES,
    ENVELOPE_KEY_LABEL,
    KEK_BYTES,
    SEAL_NONCE_BYTES,
    DeviceKeyError,
    DeviceService,
    EnvelopeKeyUnavailableError,
    derive_key_encryption_key,
    generate_envelope_key,
    seal_envelope_key,
    unseal_envelope_key,
)

pytestmark = pytest.mark.mandatory

#: Obviously synthetic and self-labelling, per `.antigravity/steering/secret-safety.md`.
PEPPER = "test-only-not-a-real-secret-envelope-pepper"
OTHER_PEPPER = "test-only-not-a-real-secret-different-pepper"


class TestTheKeyEncryptionKeyIsDerived:
    def test_it_is_deterministic_for_one_pepper(self) -> None:
        """Deterministic, or a restart could not open what the previous process sealed."""
        assert derive_key_encryption_key(PEPPER) == derive_key_encryption_key(PEPPER)

    def test_it_is_thirty_two_bytes_because_the_seal_is_aes_256(self) -> None:
        assert len(derive_key_encryption_key(PEPPER)) == KEK_BYTES == 32

    def test_a_different_pepper_derives_a_different_key(self) -> None:
        assert derive_key_encryption_key(PEPPER) != derive_key_encryption_key(OTHER_PEPPER)

    def test_str_and_bytes_peppers_agree(self) -> None:
        """UTF-8 is the one encoding, fixed here rather than at each call site."""
        assert derive_key_encryption_key(PEPPER) == derive_key_encryption_key(PEPPER.encode("utf-8"))

    def test_an_empty_pepper_is_refused_rather_than_deriving_a_well_known_key(self) -> None:
        """The failure that matters: every deployment with no pepper sharing one KEK."""
        with pytest.raises(DeviceKeyError, match="ENVELOPE_PEPPER is empty"):
            derive_key_encryption_key("")

    def test_it_is_domain_separated_from_the_peppers_hmac_use(self) -> None:
        """The whole reason one secret can serve two purposes (D-62).

        Compares against the plain `HMAC-SHA256(pepper, label)` an implementation might reach for
        if the HKDF step were dropped. They must differ, or "domain-separated" would be a word in
        a docstring rather than a property of the bytes.
        """
        import hashlib
        import hmac

        naive = hmac.new(PEPPER.encode("utf-8"), ENVELOPE_KEY_LABEL, hashlib.sha256).digest()
        assert derive_key_encryption_key(PEPPER) != naive

    def test_the_label_is_versioned(self) -> None:
        """A scheme change must be a new label, not a silent reinterpretation of old bytes."""
        assert ENVELOPE_KEY_LABEL == b"forgeops-envelope-key-v1"


class TestTheSealRoundTrips:
    def test_a_sealed_key_unseals_to_itself(self) -> None:
        kek = derive_key_encryption_key(PEPPER)
        device = uuid.uuid4()
        key = generate_envelope_key()
        assert unseal_envelope_key(seal_envelope_key(key, device_id=device, kek=kek), device_id=device, kek=kek) == key

    def test_a_generated_key_is_thirty_two_bytes(self) -> None:
        """32 bytes is HMAC-SHA256's key length used verbatim, not hashed down (§7.6)."""
        assert len(generate_envelope_key()) == ENVELOPE_KEY_BYTES == 32

    def test_the_ciphertext_carries_the_nonce_in_front(self) -> None:
        """One column, one value: a nonce in a second column could be updated independently."""
        kek = derive_key_encryption_key(PEPPER)
        sealed = seal_envelope_key(generate_envelope_key(), device_id=uuid.uuid4(), kek=kek)
        # 12-byte nonce + 32-byte plaintext + 16-byte GCM tag.
        assert len(sealed) == SEAL_NONCE_BYTES + ENVELOPE_KEY_BYTES + 16

    def test_sealing_an_empty_key_is_refused(self) -> None:
        with pytest.raises(DeviceKeyError, match="empty envelope key"):
            seal_envelope_key(b"", device_id=uuid.uuid4(), kek=derive_key_encryption_key(PEPPER))

    @pytest.mark.parametrize("length", [0, 16, 31, 33, 64])
    def test_a_wrong_length_kek_is_refused_on_both_sides(self, length: int) -> None:
        with pytest.raises(DeviceKeyError, match="key-encryption key must be"):
            seal_envelope_key(b"x" * 32, device_id=uuid.uuid4(), kek=b"k" * length)
        with pytest.raises(DeviceKeyError, match="key-encryption key must be"):
            unseal_envelope_key(b"x" * 60, device_id=uuid.uuid4(), kek=b"k" * length)


class TestTheAeadIsBoundToTheRow:
    """D-62's addition. The attack this closes needs no key material at all — only `UPDATE`."""

    def test_a_ciphertext_does_not_unseal_under_another_device(self) -> None:
        kek = derive_key_encryption_key(PEPPER)
        attacker_device, victim_device = uuid.uuid4(), uuid.uuid4()
        known_key = generate_envelope_key()
        transplanted = seal_envelope_key(known_key, device_id=attacker_device, kek=kek)

        # The transplant: the attacker writes their own ciphertext into the victim's row.
        with pytest.raises(EnvelopeKeyUnavailableError, match="did not authenticate"):
            unseal_envelope_key(transplanted, device_id=victim_device, kek=kek)

        # And the control: the same bytes still open under the row they were sealed for, so the
        # refusal above is attributable to the binding rather than to a broken ciphertext.
        assert unseal_envelope_key(transplanted, device_id=attacker_device, kek=kek) == known_key

    def test_the_aad_is_the_uuid_bytes_not_a_string_spelling(self) -> None:
        """One byte encoding, several string spellings. A seal written under one spelling and
        opened under another would fail for a reason that looks like tampering."""
        kek = derive_key_encryption_key(PEPPER)
        device = uuid.uuid4()
        sealed = seal_envelope_key(generate_envelope_key(), device_id=device, kek=kek)
        # A UUID reconstructed from its hex, its urn form and its braced form is the SAME UUID,
        # so all three must open the seal.
        for spelling in (str(device), device.hex, device.urn, f"{{{device}}}"):
            assert unseal_envelope_key(sealed, device_id=uuid.UUID(spelling), kek=kek)

    def test_a_non_uuid_device_id_is_refused_rather_than_coerced(self) -> None:
        kek = derive_key_encryption_key(PEPPER)
        with pytest.raises(DeviceKeyError, match="device_id must be a UUID"):
            seal_envelope_key(b"x" * 32, device_id="not-a-uuid", kek=kek)  # type: ignore[arg-type]

    def test_a_different_pepper_cannot_unseal(self) -> None:
        device = uuid.uuid4()
        sealed = seal_envelope_key(generate_envelope_key(), device_id=device, kek=derive_key_encryption_key(PEPPER))
        with pytest.raises(EnvelopeKeyUnavailableError, match="did not authenticate"):
            unseal_envelope_key(sealed, device_id=device, kek=derive_key_encryption_key(OTHER_PEPPER))


class TestTheNonceIsFresh:
    def test_every_seal_uses_a_fresh_nonce(self) -> None:
        """Asserted across a generated set rather than trusted (D-62's addition).

        AES-GCM nonce reuse under one key leaks the authentication subkey outright, so "the nonce
        is random" is not a claim to take on faith from a docstring. 512 seals of the SAME
        plaintext under the SAME key and the SAME device id: every nonce, and therefore every
        ciphertext, must differ.
        """
        kek = derive_key_encryption_key(PEPPER)
        device = uuid.uuid4()
        key = generate_envelope_key()
        seals = [seal_envelope_key(key, device_id=device, kek=kek) for _ in range(512)]
        nonces = {sealed[:SEAL_NONCE_BYTES] for sealed in seals}
        assert len(nonces) == 512, f"nonce collision after {512 - len(nonces)} repeats"
        assert len({bytes(sealed) for sealed in seals}) == 512

    def test_the_nonce_is_ninety_six_bits(self) -> None:
        """The length AES-GCM is specified for; any other forces a second derivation path."""
        assert SEAL_NONCE_BYTES * 8 == 96

    def test_the_nonce_is_not_derived_from_the_device_id(self) -> None:
        """A nonce derived from anything reusable repeats the moment a device is re-keyed."""
        kek = derive_key_encryption_key(PEPPER)
        device = uuid.uuid4()
        first = seal_envelope_key(generate_envelope_key(), device_id=device, kek=kek)
        second = seal_envelope_key(generate_envelope_key(), device_id=device, kek=kek)
        assert first[:SEAL_NONCE_BYTES] != second[:SEAL_NONCE_BYTES]


class TestUnsealFailuresAreIndistinguishable:
    """An attacker with database access must not learn *why* a transplant failed."""

    @pytest.mark.parametrize("mutilate", ["truncate", "flip-tag", "flip-ciphertext", "flip-nonce"])
    def test_every_corruption_reports_the_same_thing(self, mutilate: str) -> None:
        kek = derive_key_encryption_key(PEPPER)
        device = uuid.uuid4()
        sealed = bytearray(seal_envelope_key(generate_envelope_key(), device_id=device, kek=kek))
        match mutilate:
            case "truncate":
                sealed = sealed[: SEAL_NONCE_BYTES + 4]
            case "flip-tag":
                sealed[-1] ^= 0x01
            case "flip-ciphertext":
                sealed[SEAL_NONCE_BYTES] ^= 0x01
            case "flip-nonce":
                sealed[0] ^= 0x01
        with pytest.raises(EnvelopeKeyUnavailableError):
            unseal_envelope_key(bytes(sealed), device_id=device, kek=kek)

    @pytest.mark.parametrize("value", [b"", None, b"tooshort"])
    def test_a_missing_or_short_column_is_reported_as_unavailable(self, value: bytes | None) -> None:
        with pytest.raises(EnvelopeKeyUnavailableError, match="missing or truncated"):
            unseal_envelope_key(value, device_id=uuid.uuid4(), kek=derive_key_encryption_key(PEPPER))  # type: ignore[arg-type]


class TestTheServiceRefusesToRunWithoutAPepper:
    def test_construction_without_a_pepper_raises(self) -> None:
        """Fail at composition rather than at the first mint. A service constructed without a
        pepper would raise inside a governance transit, after five stages had already run."""
        with pytest.raises(DeviceKeyError, match="non-empty ENVELOPE_PEPPER"):
            DeviceService(pepper="")

    def test_the_method_and_the_banned_function_are_the_same_surface(self) -> None:
        """§11.2 writes `envelope_key` as a method; §2.2.1 bans it as a module-level name.

        A `banned-api` entry matches imports, so it can only bite a module-level symbol — an
        entry naming a method would ban nothing while looking exactly like one that does, which
        is the vacuity trap §0.4.5 exists to close. The method therefore has to delegate to the
        banned function rather than reimplement it, and this asserts the delegation exists.
        """
        import inspect

        from src.auth import devices

        source = inspect.getsource(DeviceService.envelope_key)
        assert "await envelope_key(" in source
        assert inspect.iscoroutinefunction(devices.envelope_key)
