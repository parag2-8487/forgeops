#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generate a development internal CA into `.env`, never overwriting one (design §13.4, §14.2).

What this does, and the three guarantees it owes
------------------------------------------------
`INTERNAL_CA_CERT_PEM` and `INTERNAL_CA_KEY_PEM` (§13.1) carry the development CA the backend
signs device certificates with. This writes them into the **untracked** `.env`, and:

* **it never overwrites.** If either variable already has a non-empty value, the file is left
  byte-identical and the exit status is 0 — `init-env` semantics, because a target the design
  marks idempotent must be safe to run in a loop. Overwriting would silently invalidate every
  certificate already issued, and the symptom would be agents failing their handshake with no
  connection to the command that caused it.
* **it never prints key material.** The private key goes into the file and nowhere else; stdout
  gets the certificate's fingerprint and validity, which are public.
* **it never touches a tracked file.** `.env.example` keeps its two empty placeholders. `.gitleaks`
  plus the mandatory pre-push scan in `.antigravity/steering/secret-safety.md` are the backstop, and
  §14.2 names production CA custody as **OQ-31**.

Why the generation lives in `backend/src/auth/ca.py` and not here
-----------------------------------------------------------------
`generate_development_ca()` is imported rather than reimplemented, so the CA an operator gets is
built by the same code the tests exercise. A script with its own `CertificateBuilder` chain would
be a second implementation of the thing under test, and the two would agree until the day one of
them changed — which is exactly the fixture-shaped-around-the-implementation failure the project
records as pattern F.

Why the PEM is written with `\\n` escapes
----------------------------------------
An environment variable is one line and PEM is many. The two variables therefore carry the armour
with `\\n` written as the two characters backslash-n, which `auth.ca.load_pem` normalises back.
Chosen over base64 because the value stays recognisable to a human reading `.env`: a begin-armour
line followed by `MIIB...` is obviously a certificate, and an opaque base64 blob is obviously
nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from src.auth.ca import generate_development_ca  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
CERT_KEY = "INTERNAL_CA_CERT_PEM"
KEY_KEY = "INTERNAL_CA_KEY_PEM"


def _existing_value(lines: list[str], name: str) -> str | None:
    """The current value of `name`, or `None` when it is absent.

    Matches the `NAME=value` form `.env` files use, tolerating surrounding double quotes and
    trailing whitespace. Deliberately not a full dotenv parser: this script only ever needs to
    answer "is there already a non-empty value here", and a parser that handled `export`,
    interpolation and multi-line values would be more code than the question deserves.
    """
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=\s*(.*?)\s*$")
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return None


def _replace_or_append(lines: list[str], name: str, value: str) -> list[str]:
    """Set `name` to `value`, replacing the existing line in place if there is one.

    In place rather than appended, so the ordering of `.env` keeps matching `.env.example` and a
    diff between the two stays readable. A duplicate key would also be legal in a dotenv file and
    the last one would win, which is precisely the kind of thing that is invisible in review.
    """
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
    replaced = False
    out: list[str] = []
    for line in lines:
        if pattern.match(line) and not replaced:
            out.append(f'{name}="{value}"')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'{name}="{value}"')
    return out


def main() -> int:
    if not ENV_PATH.is_file():
        print(
            f"init-ca: FAIL: {ENV_PATH.name} does not exist. Run `make init-env` first; this "
            "script writes into the untracked .env and never creates one, so it cannot be the "
            "thing that decides what else is in it.",
            file=sys.stderr,
        )
        return 1

    text = ENV_PATH.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    trailing = lines.pop() if lines and lines[-1] == "" else None

    have_cert = bool(_existing_value(lines, CERT_KEY))
    have_key = bool(_existing_value(lines, KEY_KEY))
    if have_cert and have_key:
        print(f"init-ca: {CERT_KEY} and {KEY_KEY} are already set in .env; leaving them unchanged")
        return 0
    if have_cert != have_key:
        # Refused rather than completed. Half a CA is worse than none: the present half would look
        # configured, and the resulting error would name whichever half happened to be read first.
        present, absent = (CERT_KEY, KEY_KEY) if have_cert else (KEY_KEY, CERT_KEY)
        print(
            f"init-ca: FAIL: {present} is set but {absent} is empty. A certificate and its key "
            "must be generated together. Clear both in .env and run this again.",
            file=sys.stderr,
        )
        return 1

    cert_pem, key_pem = generate_development_ca()
    escaped_cert = cert_pem.decode("ascii").replace("\n", "\\n")
    escaped_key = key_pem.decode("ascii").replace("\n", "\\n")
    lines = _replace_or_append(lines, CERT_KEY, escaped_cert)
    lines = _replace_or_append(lines, KEY_KEY, escaped_key)
    if trailing is not None:
        lines.append("")
    ENV_PATH.write_text(newline.join(lines), encoding="utf-8", newline="")

    # Public facts only: a fingerprint and a validity window. Never the key, never the certificate
    # body — an operator pasting terminal output into an issue must not be able to leak the CA.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    certificate = x509.load_pem_x509_certificate(cert_pem)
    fingerprint = ":".join(f"{byte:02X}" for byte in certificate.fingerprint(hashes.SHA256()))
    print(f"init-ca: wrote a new development CA into .env ({CERT_KEY}, {KEY_KEY})")
    print(f"init-ca: subject     {certificate.subject.rfc4514_string()}")
    print(f"init-ca: fingerprint {fingerprint}")
    print(
        f"init-ca: valid        {certificate.not_valid_before_utc.isoformat()} .. "
        f"{certificate.not_valid_after_utc.isoformat()}"
    )
    print("init-ca: the private key is in .env only; .env is git-ignored and must never be committed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
