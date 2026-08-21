#!/usr/bin/env python
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Assert the OIDC topology is reachable from BOTH sides before a journey depends on it.

WHY THIS EXISTS
The e2e overlay puts the backend and the browser on opposite sides of a network boundary. The first
attempt to reconcile them invented a hostname mapped into the container with `extra_hosts` and into
Playwright's Chromium with `--host-resolver-rules`. Every check passed and a real browser got
`DNS_PROBE_FINISHED_BAD_CONFIG`, because a name that resolves only under a launch flag does not
resolve for a person. The unit tests in `backend/tests/unit/test_oidc_public_base_url.py` now pin the
URL-building half; this pins the half that only a running stack can answer -- whether the addresses
actually resolve and answer from the side that has to use them.

Three assertions, each naming which side failed:

  1. the BACKEND CONTAINER can fetch the discovery document from `OIDC_ISSUER`;
  2. that document's `issuer` equals `OIDC_ISSUER` exactly, because a token's `iss` is verified
     against it and a near-miss fails in a way that reads as a bad token rather than a bad setting;
  3. THIS PROCESS -- standing in for the browser, on the host -- can reach the authorization endpoint
     at the public origin the backend will redirect to.

Run after the stack is up and before the journey:

    python scripts/check-oidc-reachability.py

Exits 1 with a precise message rather than a stack trace, because the failure it catches is a
topology mistake and the useful output is which of the three sides could not see the other.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

COMPOSE = [
    "docker",
    "compose",
    "-f",
    "docker-compose.yml",
    "-f",
    "docker-compose.e2e.yml",
]

DISCOVERY_SUFFIX = ".well-known/openid-configuration"


def fail(message: str) -> int:
    print(f"check-oidc-reachability: FAIL: {message}", file=sys.stderr)
    return 1


def discovery_url(issuer: str) -> str:
    return issuer if issuer.endswith(DISCOVERY_SUFFIX) else issuer.rstrip("/") + "/" + DISCOVERY_SUFFIX


def from_container(url: str) -> tuple[bool, str]:
    """Fetch from inside the backend container, which is the side that verifies `iss`."""
    script = (
        "import json,urllib.request,sys\n"
        f"d=json.load(urllib.request.urlopen({url!r}, timeout=15))\n"
        "print(json.dumps({'issuer': d.get('issuer'), 'authorization_endpoint': d.get('authorization_endpoint')}))"
    )
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*COMPOSE, "exec", "-T", "backend", "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout).strip()[-600:]
    return True, completed.stdout.strip().splitlines()[-1]


def main() -> int:
    issuer = os.environ.get("OIDC_ISSUER", "").strip()
    public_base = os.environ.get("OIDC_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not issuer:
        return fail("OIDC_ISSUER is unset; there is nothing to check")

    # ── 1 and 2: the backend's own view ─────────────────────────────────────
    ok, payload = from_container(discovery_url(issuer))
    if not ok:
        return fail(
            f"the BACKEND CONTAINER cannot fetch {discovery_url(issuer)}.\n"
            f"  OIDC_ISSUER must be an address the CONTAINER resolves -- inside Compose that is the "
            f"service name, not the host's localhost.\n  {payload}"
        )
    document = json.loads(payload)
    if document.get("issuer") != issuer:
        return fail(
            "the discovery document's issuer does not equal OIDC_ISSUER.\n"
            f"  configured: {issuer}\n  document:   {document.get('issuer')}\n"
            "  A token's `iss` is verified against the configured value, so this mismatch surfaces "
            "later as an unverifiable token rather than as a configuration error."
        )
    print(f"check-oidc-reachability: ok  backend reaches the issuer, and it matches: {issuer}")

    # ── 3: the browser's view ───────────────────────────────────────────────
    authorize = str(document.get("authorization_endpoint") or "")
    if not authorize:
        return fail("the discovery document names no authorization_endpoint")

    if public_base:
        parsed, public = urlsplit(authorize), urlsplit(public_base)
        authorize = urlunsplit((public.scheme, public.netloc, parsed.path, "", ""))
    elif urlsplit(authorize).hostname not in {"localhost", "127.0.0.1"}:
        return fail(
            f"OIDC_PUBLIC_BASE_URL is unset and the authorization endpoint is {authorize}, whose host "
            "a browser on this machine will not resolve.\n"
            "  Set OIDC_PUBLIC_BASE_URL to the published address. Do NOT solve this with a hosts "
            "entry or a browser resolver flag: those make the tests pass and leave the application "
            "broken for anyone using a normal browser."
        )

    try:
        with urllib.request.urlopen(authorize, timeout=20) as response:  # noqa: S310 — http(s) only
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code  # a 4xx from the IdP still proves it was reached
    except OSError as exc:
        return fail(
            f"a BROWSER on this host cannot reach {authorize}: {exc}\n"
            "  This is the exact failure a user sees as DNS_PROBE_FINISHED_BAD_CONFIG or a refused "
            "connection after clicking sign-in."
        )

    print(f"check-oidc-reachability: ok  a host browser reaches {authorize} (HTTP {status})")
    print("check-oidc-reachability: OK: both sides can reach the IdP at the address each was given")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
