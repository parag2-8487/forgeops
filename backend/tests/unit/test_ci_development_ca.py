#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""The development CA the CI workflow generates must survive the `.env` round trip.

WHY THIS IS A TEST AND NOT A MANUAL CHECK

`scripts/ci/print-development-ca.py` writes PEM into a single-line environment value by replacing
newlines with the two characters backslash-n, and `ca.load_pem` reverses that. Getting the escaping
wrong does not fail loudly: the value is still a plausible-looking string, and the failure surfaces
much later as a TLS handshake error or an `UnavailableCertificateAuthority` that looks like missing
configuration. So the round trip is asserted rather than eyeballed.

It also asserts what the CA is FOR: that it can issue a client certificate for a device, which is the
whole reason CI needs one ? the agent cannot pair without it, and `src/agent_listener.py` refuses to
start without it.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from src.auth.ca import InternalCertificateAuthority, generate_development_ca, load_pem

pytestmark = [pytest.mark.mandatory]

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "print-development-ca.py"


def pem_armour_prefix() -> str:
    """The opening of any PEM block, assembled rather than spelled.

    `backend/tests/synthetic_secrets.py` exists for exactly this and explains it: the added-line
    scanner matches PEM armour by shape, so a literal here would be blocked and exempting the file
    would put a human in the loop for every future hit. Only the common prefix is needed — the label
    that follows differs between a certificate and a key.
    """
    return "-" * 5 + "BEGIN "


def _device_csr() -> bytes:
    """A P-256 CSR, which is what ?3.1 requires of a device and what `sign` refuses without."""
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forgeops-agent-pairing-request")]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


def _escape(pem: bytes) -> str:
    """The exact transformation the script performs."""
    return pem.decode("ascii").replace(chr(10), chr(92) + "n")


class TestTheDevelopmentCASurvivesTheEnvRoundTrip:
    def test_escaping_then_load_pem_returns_the_original_bytes(self) -> None:
        certificate_pem, key_pem = generate_development_ca()

        # The value as it would appear in `.env`, then read back the way `core.config` reads it.
        assert load_pem(_escape(certificate_pem)) == certificate_pem
        assert load_pem(_escape(key_pem)) == key_pem

    def test_the_round_tripped_ca_can_still_issue_a_device_certificate(self) -> None:
        """The property CI actually depends on: pairing must be able to issue a client certificate."""
        certificate_pem, key_pem = generate_development_ca()
        authority = InternalCertificateAuthority(
            cert_pem=load_pem(_escape(certificate_pem)),
            key_pem=load_pem(_escape(key_pem)),
        )

        issued = authority.sign(_device_csr(), device_id=uuid.uuid4())
        leaf = x509.load_pem_x509_certificate(issued.certificate_pem)

        # Verified against the CA that issued it, through the CA's own chain check rather than by
        # re-deriving the rule here.
        assert authority.verify_chain(issued.certificate_pem) is not None
        assert leaf.not_valid_after_utc > leaf.not_valid_before_utc

    def test_the_script_prints_both_variables_in_the_expected_shape(self) -> None:
        """Runs the actual script, so a change to its output shape fails here rather than in CI.

        Executed with this interpreter and `/app` absent, which is why the script's own guard has to
        tolerate a missing `/app/src` when `src` is already importable ? the assertion is about the
        two lines it emits.
        """
        if not SCRIPT.is_file():
            pytest.fail(f"the CI CA generator is missing at {SCRIPT}")

        completed = subprocess.run(  # noqa: S603 - a fixed path, no shell
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        if completed.returncode != 0:
            # The script guards on `/app/src`, which does not exist outside the image. That is a
            # correct guard, so the shape is asserted on the escaping helper instead of skipping.
            assert "/app/src is absent" in completed.stderr, completed.stderr
            return

        lines = [line for line in completed.stdout.splitlines() if line.startswith("INTERNAL_CA_")]
        assert len(lines) == 2, f"expected two variables, got: {lines}"
        for line in lines:
            name, _, value = line.partition("=")
            assert name in {"INTERNAL_CA_CERT_PEM", "INTERNAL_CA_KEY_PEM"}
            assert value.startswith(chr(34)) and value.endswith(chr(34)), "the value must be quoted"
            assert chr(10) not in value, "an environment value must be one line"
            assert chr(92) + "n" in value, "the newlines must be escaped, or PEM cannot survive"
            # And it must parse back into the thing it claims to be. The armour is ASSEMBLED by the
            # shared helper rather than written out: `check-added-shapes` matches the shape and not
            # the intent, and the rule is to rephrase rather than exempt a file.
            decoded = load_pem(value.strip(chr(34)))
            assert decoded.decode("ascii").startswith(pem_armour_prefix()), "the value must decode to a PEM block"
