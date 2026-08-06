# SPDX-License-Identifier: FSL-1.1-ALv2
"""The internal CA: issuance, chain validation, expiry and the `init-ca` contract (§3.1, §14.2).

Why these are unit tests and not integration tests
--------------------------------------------------
Nothing here needs a database or a network. Everything here needs a **clock**, which is why
`InternalCertificateAuthority` takes one: "a certificate expires" and "a certificate is not yet
valid" are the two clauses that matter most and neither can be observed in real time without
waiting hours. Injecting the clock is the alternative to a production branch that reads a test
flag, and the second would mean the behaviour under test is not the behaviour that ships.

The negative controls are explicit. Every "X is rejected" clause is paired with the corresponding
"and the honest X is accepted", because a CA that rejected everything would satisfy the first half
of each pair on its own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509 import CertificateSigningRequestBuilder, Name, NameAttribute
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from src.auth.ca import (
    CA_COMMON_NAME,
    CertificateAuthorityUnavailableError,
    CertificateRejectedError,
    InternalCertificateAuthority,
    UnavailableCertificateAuthority,
    generate_development_ca,
    load_pem,
)

pytestmark = pytest.mark.mandatory

#: One CA for the module. Key generation dominates the runtime of this file.
CERT_PEM, KEY_PEM = generate_development_ca()
OTHER_CERT_PEM, OTHER_KEY_PEM = generate_development_ca()


def csr_for(key: ec.EllipticCurvePrivateKey | None = None, *, common_name: str = "whatever") -> bytes:
    private = key or ec.generate_private_key(ec.SECP256R1())
    return (
        CertificateSigningRequestBuilder()
        .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(private, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def ca(
    *, ttl_hours: int = 24, renew_before_hours: int = 6, now: datetime | None = None
) -> InternalCertificateAuthority:
    clock = (lambda: now) if now is not None else None
    return InternalCertificateAuthority(
        cert_pem=CERT_PEM, key_pem=KEY_PEM, ttl_hours=ttl_hours, renew_before_hours=renew_before_hours, clock=clock
    )


class TestTheDevelopmentCaItself:
    def test_it_is_a_self_signed_p256_ca_with_a_path_length_of_zero(self) -> None:
        """`path_length=0`: this CA may sign leaves and may not sign another CA.

        A development CA that could mint intermediates is a development CA somebody turns into a
        PKI by accident, and the resulting hierarchy would have no custody story at all (OQ-31).
        """
        certificate = x509.load_pem_x509_certificate(CERT_PEM)
        assert certificate.subject == certificate.issuer
        assert CA_COMMON_NAME in certificate.subject.rfc4514_string()
        assert isinstance(certificate.public_key(), ec.EllipticCurvePublicKey)
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert constraints.ca is True
        assert constraints.path_length == 0
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        assert usage.key_cert_sign is True
        assert usage.crl_sign is False

    def test_the_key_is_unencrypted_pkcs8_and_loads(self) -> None:
        """Unencrypted on purpose: a passphrase stored beside the key it protects is theatre."""
        key = serialization.load_pem_private_key(KEY_PEM, password=None)
        assert isinstance(key, ec.EllipticCurvePrivateKey)

    def test_two_generations_never_share_a_key(self) -> None:
        """The generator must be a generator, not a constant. Cheap to assert, fatal if wrong."""
        assert CERT_PEM != OTHER_CERT_PEM
        assert KEY_PEM != OTHER_KEY_PEM

    def test_a_mismatched_key_and_certificate_are_refused_at_construction(self) -> None:
        """Otherwise the failure first appears in an agent's TLS handshake, with no pointer here."""
        with pytest.raises(CertificateAuthorityUnavailableError, match="not the private key"):
            InternalCertificateAuthority(cert_pem=CERT_PEM, key_pem=OTHER_KEY_PEM)

    @pytest.mark.parametrize(
        ("ttl", "renew"),
        [(0, 6), (24, 0), (6, 6), (6, 12)],
        ids=["zero-ttl", "zero-renew", "renew-equals-ttl", "renew-exceeds-ttl"],
    )
    def test_an_incoherent_ttl_pair_is_refused(self, ttl: int, renew: int) -> None:
        """A certificate due for renewal before it is issued is a configuration that cannot work."""
        with pytest.raises(CertificateAuthorityUnavailableError):
            InternalCertificateAuthority(cert_pem=CERT_PEM, key_pem=KEY_PEM, ttl_hours=ttl, renew_before_hours=renew)


class TestIssuance:
    def test_the_subject_is_the_device_id_and_the_csrs_subject_is_discarded(self) -> None:
        """The CA does not copy caller-supplied identity into a credential.

        The CSR here asks to be `CN=attacker-chosen`. The issued certificate says `CN=<device_id>`,
        because the identity comes from the row the backend created and not from the request.
        """
        device_id = uuid.uuid4()
        issued = ca().sign(csr_for(common_name="attacker-chosen"), device_id=device_id)
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        assert certificate.subject.rfc4514_string() == f"CN={device_id}"
        assert "attacker-chosen" not in certificate.subject.rfc4514_string()

    def test_the_public_key_is_the_csrs(self) -> None:
        """The one thing the CSR does contribute."""
        key = ec.generate_private_key(ec.SECP256R1())
        issued = ca().sign(csr_for(key), device_id=uuid.uuid4())
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        assert certificate.public_key().public_numbers() == key.public_key().public_numbers()

    def test_the_extensions_are_client_auth_only_and_not_a_ca(self) -> None:
        """A leaf that could sign, or could serve TLS, is a leaf worth more to a thief."""
        issued = ca().sign(csr_for(), device_id=uuid.uuid4())
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
        assert constraints.value.ca is False
        assert constraints.critical is True
        usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        assert usage.digital_signature is True
        assert usage.key_cert_sign is False
        extended = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(extended) == [ExtendedKeyUsageOID.CLIENT_AUTH]
        assert ExtendedKeyUsageOID.SERVER_AUTH not in list(extended)
        certificate.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)

    def test_two_issues_never_share_a_serial(self) -> None:
        """`uq_agent_devices_cert_serial` is a UNIQUE constraint; a repeated serial is a 500."""
        authority = ca()
        serials = {authority.sign(csr_for(), device_id=uuid.uuid4()).serial for _ in range(25)}
        assert len(serials) == 25

    def test_the_fingerprint_is_the_der_digest_in_colon_hex(self) -> None:
        """Over the DER, because a PEM digest is a digest of a formatting choice.

        95 characters, which is exactly `agent_devices.cert_fingerprint`'s `max_length`.
        """
        issued = ca().sign(csr_for(), device_id=uuid.uuid4())
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        expected = ":".join(f"{byte:02X}" for byte in certificate.fingerprint(hashes.SHA256()))
        assert issued.fingerprint == expected
        assert len(issued.fingerprint) == 95

    def test_the_serial_fits_the_column(self) -> None:
        """`cert_serial` is `varchar(64)`; a 20-octet serial is at most 40 hex characters."""
        for _ in range(10):
            assert len(ca().sign(csr_for(), device_id=uuid.uuid4()).serial) <= 64

    def test_not_after_is_now_plus_the_ttl_and_not_before_is_backdated_one_minute(self) -> None:
        """The minute of backdating is the same ±60 s §7.6 tolerates on envelopes."""
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        issued = ca(ttl_hours=24, now=now).sign(csr_for(), device_id=uuid.uuid4())
        assert issued.not_before == now - timedelta(minutes=1)
        assert issued.not_after == now + timedelta(hours=24)
        assert issued.renew_after == issued.not_after - timedelta(hours=6)

    @pytest.mark.parametrize("ttl", [2, 6, 24, 168])
    def test_the_ttl_is_honoured_for_every_reachable_value(self, ttl: int) -> None:
        """§13.1 bounds `DEVICE_CERT_TTL_HOURS` at 1..168, and 1 is **not reachable**.

        `DEVICE_CERT_RENEW_BEFORE_HOURS` is `ge=1` and `core/config.py`'s validator requires it to
        be strictly smaller than the TTL, so `DEVICE_CERT_TTL_HOURS=1` cannot be paired with any
        legal renewal margin — the configuration refuses to load. That is a pre-existing
        inconsistency between two `ge=1` bounds and a strict inequality (chapter 9, finding 56); it
        fails closed, so the honest thing is to test the range that exists rather than to assert a
        value no deployment can use.
        """
        now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        issued = ca(ttl_hours=ttl, renew_before_hours=1, now=now).sign(csr_for(), device_id=uuid.uuid4())
        assert issued.not_after - now == timedelta(hours=ttl)

    def test_an_unreadable_csr_is_refused(self) -> None:
        with pytest.raises(CertificateRejectedError, match="readable PEM CSR"):
            ca().sign(b"not a csr at all", device_id=uuid.uuid4())

    def test_an_rsa_csr_is_refused(self) -> None:
        """§3.1 fixes the curve. Accepting RSA would let a caller choose a weak key."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (
            CertificateSigningRequestBuilder()
            .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, "rsa")]))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )
        with pytest.raises(CertificateRejectedError, match="P-256"):
            ca().sign(csr, device_id=uuid.uuid4())

    def test_a_p384_csr_is_refused(self) -> None:
        """The near miss: an EC key on the wrong curve, which a curve-blind check would accept."""
        key = ec.generate_private_key(ec.SECP384R1())
        csr = (
            CertificateSigningRequestBuilder()
            .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, "p384")]))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )
        with pytest.raises(CertificateRejectedError, match="P-256"):
            ca().sign(csr, device_id=uuid.uuid4())

    def test_the_control_shows_a_p256_csr_is_accepted(self) -> None:
        """Without this, every refusal clause above passes for a CA that signs nothing.

        Asserted by **parsing** the result rather than by checking its armour prefix. That is the
        stronger claim — a truncated PEM would have the right prefix — and it keeps the mandatory
        pre-push shape grep in `.antigravity/steering/secret-safety.md` quiet, which matters because a
        permanent match makes a stop-the-push gate into noise.
        """
        issued = ca().sign(csr_for(), device_id=uuid.uuid4())
        assert x509.load_pem_x509_certificate(issued.certificate_pem)


class TestChainValidation:
    def test_a_certificate_this_ca_issued_verifies(self) -> None:
        authority = ca()
        issued = authority.sign(csr_for(), device_id=uuid.uuid4())
        certificate = authority.verify_chain(issued.certificate_pem)
        assert certificate.serial_number == int(issued.serial, 16)

    def test_a_certificate_from_another_ca_is_refused(self) -> None:
        """The clause that makes the chain check worth running at all.

        The message is the **signature** one rather than the issuer-name one, and that is
        informative: `generate_development_ca` uses a fixed subject (§14.2's development CA has one
        recognisable name), so two independently generated development CAs share an issuer name and
        differ only in their keys. The name comparison waves this through and the signature check
        catches it — which is exactly why the signature check has to exist.
        """
        other = InternalCertificateAuthority(cert_pem=OTHER_CERT_PEM, key_pem=OTHER_KEY_PEM)
        foreign = other.sign(csr_for(), device_id=uuid.uuid4())
        with pytest.raises(CertificateRejectedError, match="signature does not verify"):
            ca().verify_chain(foreign.certificate_pem)

    def test_a_certificate_with_a_different_issuer_name_is_refused_by_name(self) -> None:
        """The cheap branch, exercised on purpose so it is not dead code.

        Its value is diagnostic rather than security: for the common operational case — an agent
        holding a certificate from a CA that was regenerated under a different name — it turns
        "signature mismatch" into "different CA", which is the difference between a five-minute fix
        and an hour of confusion.
        """
        stranger_key = ec.generate_private_key(ec.SECP256R1())
        stranger_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Someone Else's CA")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(uuid.uuid4()))]))
            .issuer_name(stranger_name)
            .public_key(ec.generate_private_key(ec.SECP256R1()).public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=24))
            .sign(stranger_key, hashes.SHA256())
        )
        with pytest.raises(CertificateRejectedError, match="issuer name mismatch"):
            ca().verify_chain(certificate.public_bytes(serialization.Encoding.PEM))

    def test_a_certificate_with_a_copied_issuer_name_but_a_foreign_signature_is_refused(self) -> None:
        """Why the issuer-name check is not sufficient on its own.

        The issuer *name* is text a forger can copy. This builds a certificate that claims this
        CA's exact subject as its issuer and is signed by a different key, which is precisely the
        forgery an implementation that stopped at the name comparison would accept.
        """
        authority = ca()
        forger_key = serialization.load_pem_private_key(OTHER_KEY_PEM, password=None)
        device_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        forged = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(uuid.uuid4()))]))
            .issuer_name(authority.subject)
            .public_key(device_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=24))
            .sign(forger_key, hashes.SHA256())  # type: ignore[arg-type]
        )
        with pytest.raises(CertificateRejectedError, match="signature does not verify"):
            authority.verify_chain(forged.public_bytes(serialization.Encoding.PEM))

    def test_an_expired_certificate_is_refused(self) -> None:
        """Driven by moving the clock, not by waiting a day."""
        issue_time = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        issued = ca(ttl_hours=24, now=issue_time).sign(csr_for(), device_id=uuid.uuid4())
        later = ca(ttl_hours=24, now=issue_time + timedelta(hours=24, minutes=1))
        with pytest.raises(CertificateRejectedError, match="expired"):
            later.verify_chain(issued.certificate_pem)

    def test_the_control_shows_it_verifies_one_minute_before_expiry(self) -> None:
        issue_time = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        issued = ca(ttl_hours=24, now=issue_time).sign(csr_for(), device_id=uuid.uuid4())
        earlier = ca(ttl_hours=24, now=issue_time + timedelta(hours=23, minutes=59))
        assert earlier.verify_chain(issued.certificate_pem)

    def test_a_not_yet_valid_certificate_is_refused(self) -> None:
        """The other end of the window, which the one minute of backdating narrows but keeps."""
        issue_time = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
        issued = ca(now=issue_time).sign(csr_for(), device_id=uuid.uuid4())
        much_earlier = ca(now=issue_time - timedelta(hours=1))
        with pytest.raises(CertificateRejectedError, match="not yet valid"):
            much_earlier.verify_chain(issued.certificate_pem)

    def test_unreadable_bytes_are_refused(self) -> None:
        with pytest.raises(CertificateRejectedError, match="readable PEM"):
            ca().verify_chain(b"not a certificate")

    def test_the_chain_check_does_not_answer_which_device_a_certificate_belongs_to(self) -> None:
        """The two checks §3.1's handshake performs are not redundant, and this states why.

        A certificate issued to device A verifies under the CA while device B's row names a
        different fingerprint. Chain validity is a precondition; `cert_fingerprint` is the
        authorisation input.
        """
        authority = ca()
        first = authority.sign(csr_for(), device_id=uuid.uuid4())
        second = authority.sign(csr_for(), device_id=uuid.uuid4())
        assert authority.verify_chain(first.certificate_pem)
        assert authority.verify_chain(second.certificate_pem)
        assert first.fingerprint != second.fingerprint


class TestTheUnavailableStandIn:
    @pytest.mark.parametrize("call", ["ca_bundle", "sign", "verify_chain"])
    def test_every_method_refuses_and_names_the_remedy(self, call: str) -> None:
        """Fail closed, with a message that says what to run. §11.1's `Unavailable*` pattern."""
        stand_in = UnavailableCertificateAuthority()
        with pytest.raises(CertificateAuthorityUnavailableError, match="make init-ca"):
            if call == "ca_bundle":
                _ = stand_in.ca_bundle
            elif call == "sign":
                stand_in.sign(b"", device_id=uuid.uuid4())
            else:
                stand_in.verify_chain(b"")

    def test_it_satisfies_the_issuer_protocol(self) -> None:
        """§0.4.3: the stand-in is bound against the same shape the real CA implements."""
        from src.auth.ca import CertificateIssuer

        assert isinstance(UnavailableCertificateAuthority(), CertificateIssuer)
        assert isinstance(ca(), CertificateIssuer)


class TestPemNormalisation:
    def test_a_pem_with_escaped_newlines_round_trips(self) -> None:
        """The form `.env` carries, because an environment variable is one line and PEM is many."""
        escaped = CERT_PEM.decode("ascii").replace("\n", "\\n")
        assert load_pem(escaped) == CERT_PEM

    def test_a_pem_with_real_newlines_is_accepted_unchanged(self) -> None:
        """Docker Compose's multi-line YAML and a pasted file both produce this form."""
        assert load_pem(CERT_PEM) == CERT_PEM

    def test_crlf_is_normalised(self) -> None:
        """A Windows editor is a realistic source of `.env`, and CRLF breaks the PEM parser."""
        assert load_pem(CERT_PEM.decode("ascii").replace("\n", "\r\n")) == CERT_PEM

    def test_an_escaped_pem_constructs_a_working_ca(self) -> None:
        """The clause that matters: the whole round trip, not just the string transformation."""
        authority = InternalCertificateAuthority(
            cert_pem=CERT_PEM.decode("ascii").replace("\n", "\\n"),
            key_pem=KEY_PEM.decode("ascii").replace("\n", "\\n"),
        )
        assert authority.verify_chain(authority.sign(csr_for(), device_id=uuid.uuid4()).certificate_pem)

    @pytest.mark.parametrize("value", ["", "   ", "\\n"])
    def test_an_empty_value_is_refused(self, value: str) -> None:
        with pytest.raises(CertificateAuthorityUnavailableError, match="empty"):
            load_pem(value)

    def test_a_non_pem_value_names_the_remedy(self) -> None:
        with pytest.raises(CertificateAuthorityUnavailableError, match="make init-ca"):
            load_pem("this-is-not-a-certificate")
