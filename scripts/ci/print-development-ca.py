#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Print a fresh development internal CA as two `.env` lines (design.md §13.1, §13.4, §14.2).

WHY THIS EXISTS ALONGSIDE `scripts/init_ca.py`

`init_ca.py` writes `.env` directly and refuses to overwrite an existing CA, which is right for a
developer's machine. CI needs the same CA in a different shape: printed to stdout so the workflow can
append it, and generated unconditionally because every run starts from a checkout with no `.env`.

WHY CI NEEDS IT AT ALL

`.env.example` ships `INTERNAL_CA_CERT_PEM` and `INTERNAL_CA_KEY_PEM` empty, deliberately — key
material does not belong in a committed file. Without them the backend composes
`UnavailableCertificateAuthority`, so the pairing exchange answers 503 and no device can be issued a
client certificate; and `src/agent_listener.py` exits 2 rather than serving a plaintext port that
would refuse every handshake for a missing certificate. The journey needs a real CA on both sides of
one mTLS connection, so the workflow generates one.

It is run inside the backend image because generating an X.509 CA needs the pinned `cryptography`,
and it reuses `generate_development_ca` rather than reimplementing it: a script with its own
generation logic is a second implementation of the thing under test.

THE ESCAPING IS THE POINT. `§13.1`'s variables carry PEM armour with newlines written as the two
characters backslash-n, because an environment variable is one line and PEM is many. `ca.load_pem`
unescapes it on the way in. Getting this wrong produces a certificate that parses as garbage, which
surfaces three layers away as a TLS handshake failure.
"""

from __future__ import annotations

import pathlib
import sys

# The backend image has `/app/src` but no `scripts/`, so this file is mounted in and has to put the
# application on the path itself.
sys.path.insert(0, "/app")
if not pathlib.Path("/app/src").is_dir():  # pragma: no cover - a wrong mount, not a code path
    print("print-development-ca: /app/src is absent; run this inside the backend image", file=sys.stderr)
    raise SystemExit(2)

from src.auth.ca import generate_development_ca  # noqa: E402 - after the sys.path insertion above

#: Newline and backslash by ordinal, so no source line here carries an escape sequence that a
#: reader has to disentangle from the surrounding quoting.
_NEWLINE = chr(10)
_ESCAPED_NEWLINE = chr(92) + "n"
_QUOTE = chr(34)


def _as_env_value(pem: bytes) -> str:
    """One PEM block as a quoted, single-line environment value."""
    return _QUOTE + pem.decode("ascii").replace(_NEWLINE, _ESCAPED_NEWLINE) + _QUOTE


def main() -> int:
    certificate_pem, key_pem = generate_development_ca()
    print("INTERNAL_CA_CERT_PEM=" + _as_env_value(certificate_pem))
    print("INTERNAL_CA_KEY_PEM=" + _as_env_value(key_pem))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
