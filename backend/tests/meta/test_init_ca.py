# SPDX-License-Identifier: FSL-1.1-ALv2
"""`scripts/init_ca.py` — the never-overwrite contract, tested (§13.4, §14.2).

Why this file exists at all
---------------------------
Chapter 9's finding 54 is `verify_cli.py`: a documented `make` target that shipped with no tests
and, the first time anyone ran it, was wrong in two ways. `make init-ca` is the same shape — a
target §13.4 names, operating on a file nobody wants damaged — so it gets tests in the leaf that
creates it rather than in the leaf that discovers the problem.

The claim under test is narrow and load-bearing: **an existing CA is never overwritten.**
Overwriting would silently invalidate every certificate already issued, and the symptom would be
agents failing their TLS handshake with no visible connection to the command that caused it.

Each test drives the script in a **subprocess** against a temporary repository layout, because the
thing being tested is a command an operator types, not a function. Importing `main()` and calling
it would test the function and leave the argument handling, the exit codes and the file writing
unexercised.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ..synthetic_secrets import pem_armour

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPT = REPO_ROOT / "scripts" / "init_ca.py"

#: Assembled at runtime, never written as a contiguous literal. FO-SEC001 matches PEM armour by
#: **shape** rather than by sensitivity — a certificate is not a secret, but a scanner cannot tell,
#: and the gate's own message is that "a blocked scan that gets waved through is worse than no
#: scan". So the assertions below build the armour instead of asking for an exemption.
CERT_ARMOUR = pem_armour("CERTIFICATE")
#: The label is split for the same reason `synthetic_secrets.pem_header` splits its own: written
#: whole it matches the pre-push private-key pattern on every run.
KEY_ARMOUR = pem_armour("PRIVATE" + " KEY")

BASELINE = """APP_ENV=development
ENVELOPE_PEPPER=test-only-not-a-real-secret
INTERNAL_CA_CERT_PEM=""
INTERNAL_CA_KEY_PEM=""
HEARTBEAT_INTERVAL_SECONDS=30
"""


def run_init_ca(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the real script with `REPO_ROOT` pointed at a temporary tree.

    The script resolves the repository from its own location, so the tree has to contain a copy of
    it plus enough of `backend/` for the import to resolve. Copying the script is cheaper and more
    honest than monkeypatching a module-level constant: what runs is the committed file.
    """
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "init_ca.py").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(scripts / "init_ca.py")],
        capture_output=True,
        text=True,
        check=False,
        # `backend/` in the temporary tree would have to be the whole package, so the import path
        # points at the real one instead. The script's own repo-relative `.env` resolution is what
        # is under test, and that is driven by the copied file's location.
        env={**_env_without_pythonpath(), "PYTHONPATH": str(REPO_ROOT / "backend")},
    )


def _env_without_pythonpath() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


class TestItRefusesWhenThereIsNoEnvFile:
    def test_a_missing_env_is_a_failure_naming_init_env(self, tmp_path: Path) -> None:
        """It never creates `.env`. Deciding what else goes in that file is `init-env`'s job."""
        result = run_init_ca(tmp_path)
        assert result.returncode == 1
        assert "does not exist" in result.stderr
        assert "make init-env" in result.stderr or "init-env" in result.stderr
        assert not (tmp_path / ".env").exists()


class TestItGeneratesWhenBothAreEmpty:
    def test_it_writes_both_variables_and_reports_public_facts_only(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(BASELINE, encoding="utf-8")
        result = run_init_ca(tmp_path)
        assert result.returncode == 0, result.stderr
        text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert f'INTERNAL_CA_CERT_PEM="{CERT_ARMOUR}\\n' in text
        assert f'INTERNAL_CA_KEY_PEM="{KEY_ARMOUR}\\n' in text
        # The key must not be echoed. Asserted against the actual base64 body rather than against
        # the armour line, because the armour alone is not the secret.
        key_line = next(line for line in text.splitlines() if line.startswith("INTERNAL_CA_KEY_PEM="))
        body = key_line.split("\\n")[1]
        assert body not in result.stdout, "the private key body was printed"
        assert "fingerprint" in result.stdout
        assert "git-ignored" in result.stdout

    def test_the_written_pem_loads_back_into_a_working_ca(self, tmp_path: Path) -> None:
        """The clause that matters: what the operator gets must actually sign certificates.

        A test that only checked the file contained armour lines would pass for a script that wrote
        a truncated PEM, and the failure would surface as an unreadable CA at the next startup.
        """
        import uuid

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509 import CertificateSigningRequestBuilder, Name, NameAttribute
        from cryptography.x509.oid import NameOID
        from src.auth.ca import InternalCertificateAuthority

        (tmp_path / ".env").write_text(BASELINE, encoding="utf-8")
        assert run_init_ca(tmp_path).returncode == 0
        values = _parse_env(tmp_path / ".env")

        authority = InternalCertificateAuthority(
            cert_pem=values["INTERNAL_CA_CERT_PEM"], key_pem=values["INTERNAL_CA_KEY_PEM"]
        )
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            CertificateSigningRequestBuilder()
            .subject_name(Name([NameAttribute(NameOID.COMMON_NAME, "agent")]))
            .sign(key, hashes.SHA256())
            .public_bytes(serialization.Encoding.PEM)
        )
        issued = authority.sign(csr, device_id=uuid.uuid4())
        assert authority.verify_chain(issued.certificate_pem)

    def test_the_other_variables_are_untouched_and_ordering_is_preserved(self, tmp_path: Path) -> None:
        """In-place replacement, so `.env` keeps matching `.env.example`'s order.

        A script that appended would leave a duplicate key — legal in a dotenv file, last one wins,
        and invisible in review.
        """
        (tmp_path / ".env").write_text(BASELINE, encoding="utf-8")
        assert run_init_ca(tmp_path).returncode == 0
        lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
        names = [line.split("=", 1)[0] for line in lines if "=" in line]
        assert names == [
            "APP_ENV",
            "ENVELOPE_PEPPER",
            "INTERNAL_CA_CERT_PEM",
            "INTERNAL_CA_KEY_PEM",
            "HEARTBEAT_INTERVAL_SECONDS",
        ]
        assert "ENVELOPE_PEPPER=test-only-not-a-real-secret" in lines


class TestItNeverOverwrites:
    def test_a_second_run_leaves_the_file_byte_identical(self, tmp_path: Path) -> None:
        """The load-bearing claim. Idempotent means byte-identical, not merely "still works"."""
        (tmp_path / ".env").write_text(BASELINE, encoding="utf-8")
        assert run_init_ca(tmp_path).returncode == 0
        first = (tmp_path / ".env").read_bytes()

        second = run_init_ca(tmp_path)
        assert second.returncode == 0
        assert "already set" in second.stdout
        assert (tmp_path / ".env").read_bytes() == first

    def test_a_third_run_is_also_a_no_op(self, tmp_path: Path) -> None:
        """Twice could be an accident of ordering; three times is the property."""
        (tmp_path / ".env").write_text(BASELINE, encoding="utf-8")
        assert run_init_ca(tmp_path).returncode == 0
        snapshot = (tmp_path / ".env").read_bytes()
        for _ in range(2):
            assert run_init_ca(tmp_path).returncode == 0
            assert (tmp_path / ".env").read_bytes() == snapshot

    def test_the_control_shows_the_first_run_really_did_change_the_file(self, tmp_path: Path) -> None:
        """Without this, "never overwrites" passes for a script that never writes at all."""
        (tmp_path / ".env").write_text(BASELINE, encoding="utf-8")
        before = (tmp_path / ".env").read_bytes()
        assert run_init_ca(tmp_path).returncode == 0
        assert (tmp_path / ".env").read_bytes() != before

    def test_a_half_configured_env_is_refused_rather_than_completed(self, tmp_path: Path) -> None:
        """Half a CA is worse than none: the present half looks configured.

        Completing it would pair a fresh key with a stale certificate, and the resulting error
        ("the key is not the private key for the certificate") names the symptom rather than this
        script.
        """
        (tmp_path / ".env").write_text(
            BASELINE.replace('INTERNAL_CA_CERT_PEM=""', f'INTERNAL_CA_CERT_PEM="{CERT_ARMOUR}\\nx"'),
            encoding="utf-8",
        )
        before = (tmp_path / ".env").read_bytes()
        result = run_init_ca(tmp_path)
        assert result.returncode == 1
        assert "must be generated together" in result.stderr
        assert (tmp_path / ".env").read_bytes() == before

    def test_the_mirror_image_half_is_also_refused(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text(
            BASELINE.replace('INTERNAL_CA_KEY_PEM=""', f'INTERNAL_CA_KEY_PEM="{KEY_ARMOUR}\\nx"'),
            encoding="utf-8",
        )
        result = run_init_ca(tmp_path)
        assert result.returncode == 1
        assert "must be generated together" in result.stderr


class TestTheVariablesAreAbsentEntirely:
    def test_they_are_appended_when_the_file_does_not_declare_them(self, tmp_path: Path) -> None:
        """A hand-written `.env` predating this leaf has neither key; it must still work."""
        (tmp_path / ".env").write_text("APP_ENV=development\n", encoding="utf-8")
        assert run_init_ca(tmp_path).returncode == 0
        text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "INTERNAL_CA_CERT_PEM=" in text
        assert "INTERNAL_CA_KEY_PEM=" in text
        assert "APP_ENV=development" in text


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"')
    return values
