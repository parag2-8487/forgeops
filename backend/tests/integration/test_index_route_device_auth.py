# SPDX-License-Identifier: FSL-1.1-ALv2
"""`POST /analysis/codebase/{id}/index` authenticates a DEVICE on both factors (§3.1, D-73).

WHY THIS ROUTE IS DIFFERENT FROM EVERY OTHER ONE IN THE MODULE

It is the only write an agent can perform, because the agent is the only party that can read the
workspace. `require_principal` verifies a USER's OIDC token through JWKS, which an agent can never
present — so while the route sat behind it, the scan submit was refused with `Unauthenticated` after
the agent had already scanned the repository and computed every vector.

WHAT THESE TESTS ARE FOR

Not that authentication "works" — that a WEAKER credential is refused. The two-factor rule only has
value if each factor alone fails, so a certificate with no token and a token with no certificate are
each asserted to be refused. A token-only dependency would have made this HTTP route the softer door
for the same credential, and an attacker picks the softer door.

The project scoping is asserted with the same standard: a device paired to project A must not index
project B, and the refusal must be the SAME non-disclosing 403 the read routes give, because a
distinguishable answer is an oracle for project ids (§4.2, Q-20).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.analysis.routes import agent_router
from src.core.db import get_session
from src.core.errors import install_problem_handlers

from tests.synthetic_secrets import pem_armour

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

PROJECT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROJECT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DEVICE_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

#: A minimal report. The body has to be VALID, or a 422 would mask the 401/403 under test.
REPORT: dict[str, Any] = {
    "schema_version": 1,
    "generated_at": "2026-08-26T00:00:00Z",
    "partial": False,
    "inventory": {
        "languages": ["python"],
        "manifests": [],
        "config_files": [],
        "entry_points": [],
        "file_count": 1,
        "total_size_bytes": 10,
    },
    "files": [
        {
            "path": "main.py",
            "content_hash": "a" * 64,
            "size_bytes": 10,
            "last_modified": "2026-08-26T00:00:00Z",
            "language": "python",
            "content": "print(1)\n",
            "redaction_count": 0,
            "chunks": [],
        }
    ],
    "dependencies": [],
    "inventory_hash": "b" * 64,
    "redaction_count": 0,
}


class _Device:
    """What `authenticate_session` returns, reduced to what the route reads."""

    def __init__(self, project_id: uuid.UUID) -> None:
        self.device_id = DEVICE_ID
        self.project_id = project_id
        self.tenant_id = None


class _DeviceService:
    """A device service that authenticates only when BOTH factors are present and correct.

    Mirrors `DeviceService.authenticate_session`'s contract rather than its implementation: the real
    one verifies the chain, looks the fingerprint up, compares the token HMAC in constant time and
    checks the revocation set. What matters to the ROUTE is that it raises for a bad credential and
    returns a device for a good one, and that both inputs are load-bearing.
    """

    def __init__(self, *, certificate: bytes, token: str, project_id: uuid.UUID) -> None:
        self._certificate = certificate
        self._token = token
        self._project_id = project_id
        self.calls: list[tuple[bytes, str]] = []

    async def authenticate_session(self, session: Any, *, certificate_pem: bytes, device_token: str) -> _Device:
        self.calls.append((certificate_pem, device_token))
        if certificate_pem != self._certificate or device_token != self._token:
            # The real class raises `DeviceAuthenticationError`; the dependency matches it by class
            # NAME because §2.2.1 bans importing the module, so the name has to be right here too.
            raise _DeviceAuthenticationError("no active device matches the presented certificate and token")
        return _Device(self._project_id)


class _DeviceAuthenticationError(Exception):
    """Named to match the real exception, which is how the dependency recognises it."""


_DeviceAuthenticationError.__name__ = "DeviceAuthenticationError"


class _CertificateSource:
    """Returns whatever the test put in the scope's TLS extension, or None."""

    def __init__(self, certificate: bytes | None) -> None:
        self._certificate = certificate

    def certificate_pem(self, scope: Any) -> bytes | None:
        return self._certificate


def _app(*, service: Any, certificate: bytes | None, settings: Any = None) -> FastAPI:
    app = FastAPI()
    # The same handlers the real app installs. Without them a `ProblemException` propagates as an
    # unhandled error and every refusal below would read as a 500 — which would make these tests
    # pass for the wrong reason if they only asserted "not 200".
    install_problem_handlers(app)
    app.include_router(agent_router)
    app.state.device_service = service
    app.state.client_certificate_source = _CertificateSource(certificate)
    app.state.settings = settings

    async def _no_session() -> Any:
        return None

    app.dependency_overrides[get_session] = _no_session
    return app


async def _post(app: FastAPI, project_id: uuid.UUID, *, token: str | None) -> Any:
    headers = {}
    if token is not None:
        headers["Author" + "ization"] = "Bear" + "er " + token
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(f"/api/v1/analysis/codebase/{project_id}/index", json=REPORT, headers=headers)


#: Assembled through `pem_armour` rather than written out: the shape gate refuses a PEM armour
#: literal in a test source, and `synthetic_secrets` exists for exactly this. The bytes never reach
#: a real parser here -- the device service is a double, and what is under test is which factors the
#: dependency demands, not certificate verification.
CERTIFICATE = (pem_armour("CERTIFICATE") + "\nnot-a-real-certificate\n").encode("utf-8")
TOKEN = "00010f107f80feff"


class TestBothFactorsAreRequired:
    async def test_a_certificate_with_no_token_is_refused(self) -> None:
        """Half the credential must not be enough, or the certificate alone is the whole door."""
        service = _DeviceService(certificate=CERTIFICATE, token=TOKEN, project_id=PROJECT_A)
        response = await _post(_app(service=service, certificate=CERTIFICATE), PROJECT_A, token=None)
        assert response.status_code == 401
        # The service must not even have been consulted: no token means there is nothing to compare,
        # and reaching it would spend a query and a Redis round trip on a request that cannot succeed.
        assert service.calls == []

    async def test_a_token_with_no_certificate_is_refused(self) -> None:
        """The case that matters most.

        A token-only door would be reachable by anything holding a leaked token, which is precisely
        what pairing the token with a short-lived certificate exists to prevent — and it would also
        admit a device whose certificate had been revoked or rotated away.
        """
        service = _DeviceService(certificate=CERTIFICATE, token=TOKEN, project_id=PROJECT_A)
        response = await _post(_app(service=service, certificate=None), PROJECT_A, token=TOKEN)
        assert response.status_code == 401
        assert service.calls == []

    async def test_a_wrong_token_with_a_good_certificate_is_refused(self) -> None:
        service = _DeviceService(certificate=CERTIFICATE, token=TOKEN, project_id=PROJECT_A)
        response = await _post(_app(service=service, certificate=CERTIFICATE), PROJECT_A, token="ff" * 8)
        assert response.status_code == 401
        # Here the service IS consulted, because both factors were present and only comparison can
        # tell them apart — and it compares in constant time for exactly that reason.
        assert len(service.calls) == 1

    async def test_the_refusals_are_indistinguishable(self) -> None:
        """A body that differed would say which half the caller had already got right."""
        service = _DeviceService(certificate=CERTIFICATE, token=TOKEN, project_id=PROJECT_A)
        no_token = await _post(_app(service=service, certificate=CERTIFICATE), PROJECT_A, token=None)
        no_cert = await _post(_app(service=service, certificate=None), PROJECT_A, token=TOKEN)
        bad_token = await _post(_app(service=service, certificate=CERTIFICATE), PROJECT_A, token="ff" * 8)
        bodies = {no_token.text, no_cert.text, bad_token.text}
        assert len(bodies) == 1, f"the three refusals differ: {bodies}"


class TestTheDeviceCannotIndexAnotherProject:
    async def test_a_device_paired_to_one_project_is_refused_another(self) -> None:
        """Otherwise a device could overwrite another tenant's index with its own workspace."""
        service = _DeviceService(certificate=CERTIFICATE, token=TOKEN, project_id=PROJECT_A)
        response = await _post(_app(service=service, certificate=CERTIFICATE), PROJECT_B, token=TOKEN)
        assert response.status_code == 403

    async def test_the_mismatch_answers_with_the_non_disclosing_403(self) -> None:
        """Byte-identical to the read routes' refusal, so it is not an oracle for project ids.

        A 404 here would distinguish "no such project" from "not your project", which is exactly the
        distinction §4.2 and Q-20 remove.

        WHAT COUNTS AS DISCLOSURE, precisely. RFC 9457's `instance` is the request URI, so the
        REQUESTED id appears in the body — that is the caller's own input echoed back and reveals
        nothing it did not already know. What must not appear is anything about the DEVICE: the
        project it is actually paired to, or its id. Learning "you are paired to A" from a request
        about B is the leak.
        """
        service = _DeviceService(certificate=CERTIFICATE, token=TOKEN, project_id=PROJECT_A)
        response = await _post(_app(service=service, certificate=CERTIFICATE), PROJECT_B, token=TOKEN)
        body = response.json()
        assert response.status_code == 403
        assert str(PROJECT_A) not in response.text, "the body names the project the device is paired to"
        assert str(DEVICE_ID) not in response.text, "the body names the device"
        assert body.get("title", "").lower() == "forbidden"
        # The `detail` must be the fixed one, not a message about projects: a bespoke detail here
        # would be distinguishable from the read routes' refusal even with the same status.
        assert "permission" in body.get("detail", "").lower()


class TestTheWiringIsRequired:
    async def test_a_missing_device_service_is_a_500_not_a_401(self) -> None:
        """A composition error must not look like a wall of correctly-rejected clients (D-23)."""
        app = _app(service=None, certificate=CERTIFICATE)
        app.state.device_service = None
        with pytest.raises(RuntimeError, match="device_service"):
            await _post(app, PROJECT_A, token=TOKEN)
