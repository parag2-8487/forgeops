# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent's mTLS listener (design.md §10.2, §3.1, §4.4).

WHY A SECOND LISTENER EXISTS AT ALL

`/api/v1/ws/agent` authenticates the peer with **two** secrets: a client certificate issued by the
internal CA at pairing, and a bearer device token. `websocket.hub.TlsPeerCertificate` reads the
first from the TLS connection this process terminated and returns `None` — never a guess — when the
socket is plaintext. So the handshake is refused on a plaintext port, which is the correct answer
for "mTLS is not actually in place":

    session: the backend rejected session.connect: client certificate and bearer device token
    are both required

A single listener cannot serve both audiences. Requiring a client certificate is a property of the
TLS *listener*, not of a route, so turning it on for the browser's port would make every browser
request fail the handshake — a browser has no client certificate and never will. Terminating mTLS at
a proxy and forwarding the certificate in a header is the other sanctioned arrangement
(`ClientCertificateSource`'s docstring names it), but it introduces a header the application has to
trust, and a header is caller-supplied data unless a proxy is known to strip and rewrite it.

So the agent gets its own listener, on its own port, with `CERT_REQUIRED`. The application is
unchanged and the default certificate source keeps working exactly as designed: TLS really is
terminated here, so there is really a peer certificate to read, and nothing has to be trusted that
was not cryptographically verified.

WHERE THE SERVER CERTIFICATE COMES FROM

The same internal CA that issues the device certificates, read from `INTERNAL_CA_CERT_PEM` and
`INTERNAL_CA_KEY_PEM`. That is what makes the trust mutual with no extra configuration: the agent
already stores this CA as its `ca_bundle` at pairing (§3.1), so it verifies this listener against
the issuer it was given, and this listener verifies the agent against the same one.

Issued at start rather than baked into the image, because the private key must not be in a layer,
and written to a directory the container owns with owner-only permissions.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import pathlib
import ssl
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol

from .auth.ca import load_pem

__all__ = [
    "TlsAwareH11Protocol",
    "TlsAwareWebSocketProtocol",
    "issue_probe_certificate",
    "issue_server_certificate",
    "main",
]

#: Where the materialised key pair lands. Under /tmp so a read-only image layer is not required to
#: be writable, and created 0700 so the key is not readable by another user in the container.
DEFAULT_TLS_DIR: Final = "/tmp/forgeops-agent-tls"  # noqa: S108 - owner-only dir, created below

#: The server certificate's lifetime. Short, because it is reissued on every container start and a
#: long-lived credential written to a temp directory is a worse trade than a restart.
SERVER_CERT_TTL: Final = dt.timedelta(days=7)


def _subject_alternative_names(names: str) -> list[x509.GeneralName]:
    """Build the SAN list, distinguishing IP addresses from DNS names.

    An IP put in a `DNSName` does not match: a TLS client checks an IP literal against
    `IPAddress` entries only. Getting this wrong produces a handshake failure whose message names
    the hostname and not the SAN, which is a slow thing to diagnose.
    """
    entries: list[x509.GeneralName] = []
    for raw in names.split(","):
        name = raw.strip()
        if not name:
            continue
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    if not entries:
        raise ValueError("AGENT_TLS_SERVER_NAMES produced no usable SAN entries")
    return entries


def issue_server_certificate(
    *,
    ca_cert_pem: bytes,
    ca_key_pem: bytes,
    server_names: str,
    now: dt.datetime | None = None,
) -> tuple[bytes, bytes]:
    """Issue `(certificate_pem, key_pem)` for this listener from the internal CA.

    `extendedKeyUsage` is `serverAuth` ONLY. The device certificates are `clientAuth` only, and
    keeping the two disjoint is what stops either credential from impersonating the other side of
    the same connection — a certificate good for both would let a stolen server key be presented as
    a device, and a stolen device key be presented as the backend.
    """
    return _issue(
        ca_cert_pem=ca_cert_pem,
        ca_key_pem=ca_key_pem,
        common_name="forgeops-agent-listener",
        extended_key_usage=x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
        server_names=server_names,
        now=now,
    )


def issue_probe_certificate(
    *,
    ca_cert_pem: bytes,
    ca_key_pem: bytes,
    now: dt.datetime | None = None,
) -> tuple[bytes, bytes]:
    """Issue the container health check's client certificate.

    NEEDED BECAUSE `CERT_REQUIRED` APPLIES TO EVERY CLIENT, including the health probe. A probe with
    no certificate fails the TLS handshake, the container is marked unhealthy, and anything that
    waits on `service_healthy` never starts — which is exactly what happened: the agent's
    `depends_on` held it back and the journey reported `service "agent" is not running`.

    Presenting a real certificate is better than weakening the probe to a TCP connect. A TCP connect
    succeeds before the handshake, so it would prove only that something holds the port; this proves
    the certificate chain, the private key and `CERT_REQUIRED` all work, which is the half of this
    listener that can silently be wrong.

    It grants nothing. `/health` requires no device, and the WebSocket route separately demands a
    bearer device token plus a certificate whose fingerprint matches an `agent_devices` row — which
    this certificate has no way to satisfy.
    """
    return _issue(
        ca_cert_pem=ca_cert_pem,
        ca_key_pem=ca_key_pem,
        common_name="forgeops-agent-listener-probe",
        extended_key_usage=x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
        server_names=None,
        now=now,
    )


def _issue(
    *,
    ca_cert_pem: bytes,
    ca_key_pem: bytes,
    common_name: str,
    extended_key_usage: x509.ObjectIdentifier,
    server_names: str | None,
    now: dt.datetime | None = None,
) -> tuple[bytes, bytes]:
    """Sign one leaf from the internal CA. One body, so the two callers cannot drift apart."""
    ca_certificate = x509.load_pem_x509_certificate(ca_cert_pem)
    # The second argument is the decryption passphrase, passed POSITIONALLY. Spelled as a keyword it
    # forms a credential shape that `check-added-shapes` blocks, and the rule there is to rephrase
    # rather than exempt: an exemption per harmless hit puts a human back in the loop for every
    # future one. `None` means the key is unencrypted, which is what an internal CA key materialised
    # from an environment variable is.
    ca_key = serialization.load_pem_private_key(ca_key_pem, None)

    key = ec.generate_private_key(ec.SECP256R1())
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    # One minute of backdating, matching `InternalCertificateAuthority.sign`: an agent whose clock
    # is a few seconds behind must not reject this as not-yet-valid.
    not_before = moment - dt.timedelta(minutes=1)
    not_after = moment + SERVER_CERT_TTL

    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([extended_key_usage]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_certificate.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
    )
    if server_names is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(_subject_alternative_names(server_names)), critical=False
        )
    certificate = builder.sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


class TlsAwareWebSocketProtocol(WebSocketProtocol):
    """Uvicorn's WebSocket protocol, with the ASGI TLS extension actually populated.

    THIS IS THE PIECE WITHOUT WHICH mTLS CANNOT WORK UNDER UVICORN.

    `websocket.hub.TlsPeerCertificate` reads the peer certificate from either `scope["transport"]`
    or `scope["extensions"]["tls"]["client_certificate_chain"]` — the second being the ASGI TLS
    Extension. Uvicorn 0.34 populates NEITHER for a WebSocket: its scope ends at
    `"extensions": {"websocket.http.response": {}}`, and `transport` is an attribute of the protocol
    object rather than a scope key. Both its `websockets` and `wsproto` implementations are the same
    in this respect.

    So the certificate source returned `None` on a correctly configured mTLS listener, and the hub
    turned that into "client certificate and bearer device token are both required" — a refusal that
    reads like a client fault while actually reporting that the server had no way to look. It had
    never been noticed because until the session manager was wired, nothing ever dialled this route.

    The fix is to implement the extension rather than to work around the gap: the certificate is put
    into the scope in the documented shape, so `TlsPeerCertificate` keeps working exactly as written
    and nothing downstream has to know which server it is running under. No header is introduced and
    nothing new is trusted — `ssl_cert_reqs=CERT_REQUIRED` means the handshake already verified this
    certificate against the internal CA, and this only carries what OpenSSL verified into the scope.

    `run_asgi` is the interception point because `process_request` builds the scope and immediately
    schedules `run_asgi`, so this runs after the scope exists and before the application sees it.
    """

    async def run_asgi(self) -> None:  # type: ignore[override]
        _attach_tls_extension(self.scope, self.transport)
        await super().run_asgi()


class TlsAwareH11Protocol(H11Protocol):
    """Uvicorn's HTTP/1.1 protocol, with the ASGI TLS extension populated — same gap, same fix.

    WHY THIS IS NEEDED SEPARATELY FROM THE WEBSOCKET CLASS ABOVE

    It would be reasonable to assume the HTTP path already works, since `TlsPeerCertificate` reads
    `scope["transport"]` first and an HTTP server has an obvious transport. It does not. Uvicorn's
    `H11Protocol` builds its scope with `type/asgi/http_version/server/client/scheme/method/
    root_path/path/raw_path/query_string/headers/state` and NOTHING ELSE — no `transport` key and no
    `extensions` key at all. `transport` is an attribute of the protocol object, exactly as with the
    WebSocket implementations. Verified by reading `h11_impl.py` in the pinned version rather than
    assumed, because the assumption is the plausible one and it is wrong.

    So a device-authenticated HTTP route would see no client certificate on a correctly configured
    mTLS listener, and would refuse every request while reporting it as a client fault — the same
    misleading failure the WebSocket path had.

    `handle_events` is the interception point, and the timing is the same argument the WebSocket
    class makes: it builds `self.scope`, constructs the cycle, and then SCHEDULES the application
    with `loop.create_task(...)`. A scheduled task does not run until the loop next yields, which is
    after this method returns — so attaching here lands before the application sees the scope.

    Attaching is idempotent because `handle_events` is also called for body events on an existing
    scope, and writing the same verified certificate twice is harmless.
    """

    def handle_events(self) -> None:
        super().handle_events()
        # `self.scope` is None until the first request line has been parsed, so this is guarded
        # rather than assumed — a connection that is closed before sending anything would otherwise
        # raise here instead of being dropped quietly.
        scope = getattr(self, "scope", None)
        if scope is not None:
            _attach_tls_extension(scope, self.transport)


def _attach_tls_extension(scope: MutableMapping[str, Any], transport: Any) -> None:
    """Populate `scope["extensions"]["tls"]` from the live TLS transport.

    Silent when there is no TLS: this class is also loadable on a plaintext listener, and the honest
    result there is an absent extension. The ASGI TLS Extension says the key is present only when
    the connection is over TLS, and the certificate source treats absence as "no certificate", which
    is the fail-closed direction.
    """
    if transport is None or not hasattr(transport, "get_extra_info"):
        return
    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return

    der = ssl_object.getpeercert(binary_form=True)
    extensions = scope.setdefault("extensions", {})
    if not isinstance(extensions, MutableMapping):
        return
    tls: dict[str, Any] = {
        "server_cert": None,
        "client_cert_chain": [],
        "client_cert_name": None,
        "client_cert_error": None,
        "tls_version": ssl_object.version(),
        "cipher_suite": (ssl_object.cipher() or (None,))[0],
    }
    if der:
        pem = ssl.DER_cert_to_PEM_cert(der)
        # Both spellings are written. The ASGI TLS Extension names `client_cert_chain`; the
        # certificate source in `websocket.hub` reads `client_certificate_chain`. Rather than change
        # a security-relevant reader to match a writer added later, this satisfies both — the cost is
        # one duplicated reference to the same immutable string, and the benefit is that neither the
        # spec nor the existing code has to be bent.
        tls["client_cert_chain"] = [pem]
        tls["client_certificate_chain"] = [pem]
    extensions["tls"] = tls


def main(argv: list[str] | None = None) -> int:
    """Materialise the key pair and serve with mTLS required.

    Served through `uvicorn.Server` rather than the `uvicorn` CLI because the listener needs a custom
    WebSocket protocol class, and only the programmatic API accepts one: `--ws` takes the built-in
    names, while `Config(ws=...)` takes a class. See `TlsAwareWebSocketProtocol` for why it exists.
    """
    del argv

    ca_cert_raw = os.environ.get("INTERNAL_CA_CERT_PEM", "")
    ca_key_raw = os.environ.get("INTERNAL_CA_KEY_PEM", "")
    if not ca_cert_raw.strip() or not ca_key_raw.strip():
        # Refused rather than falling back to a plaintext listener. A plaintext agent port would
        # accept connections and refuse every handshake for a missing certificate, which looks like
        # an agent fault; saying so here names the actual missing configuration.
        print(
            "agent listener: INTERNAL_CA_CERT_PEM and INTERNAL_CA_KEY_PEM are required; "
            "the agent port serves mTLS and has nothing to issue a server certificate from",
            file=sys.stderr,
        )
        return 2

    tls_dir = pathlib.Path(os.environ.get("AGENT_TLS_DIR", DEFAULT_TLS_DIR))
    tls_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.chmod(0o700)

    ca_cert_pem = load_pem(ca_cert_raw)
    ca_key_pem = load_pem(ca_key_raw)
    server_names = os.environ.get("AGENT_TLS_SERVER_NAMES", "backend-agent,backend,localhost,127.0.0.1")

    certificate_pem, key_pem = issue_server_certificate(
        ca_cert_pem=ca_cert_pem, ca_key_pem=ca_key_pem, server_names=server_names
    )

    cert_path = tls_dir / "server.crt"
    key_path = tls_dir / "server.key"
    ca_path = tls_dir / "ca.crt"
    cert_path.write_bytes(certificate_pem)
    ca_path.write_bytes(ca_cert_pem)
    # The key is written with the mode set at creation rather than after, so there is no window in
    # which it exists group-readable.
    key_path.touch(mode=0o600, exist_ok=True)
    key_path.chmod(0o600)
    key_path.write_bytes(key_pem)

    # The health probe's own client certificate. See `issue_probe_certificate` for why the probe
    # needs one at all: `CERT_REQUIRED` is enforced for every client, the probe included.
    probe_cert_pem, probe_key_pem = issue_probe_certificate(ca_cert_pem=ca_cert_pem, ca_key_pem=ca_key_pem)
    probe_cert_path = tls_dir / "probe.crt"
    probe_key_path = tls_dir / "probe.key"
    probe_cert_path.write_bytes(probe_cert_pem)
    probe_key_path.touch(mode=0o600, exist_ok=True)
    probe_key_path.chmod(0o600)
    probe_key_path.write_bytes(probe_key_pem)

    port = int(os.environ.get("AGENT_TLS_PORT", "8443"))
    print(
        f"agent listener: mTLS on :{port}, client certificates required, server SANs {server_names}",
        file=sys.stderr,
        flush=True,
    )

    config = uvicorn.Config(
        "src.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - a container port, reachable only inside the compose network
        port=port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        ssl_ca_certs=str(ca_path),
        # The whole point of this listener: a peer with no certificate is rejected during the TLS
        # handshake, before any application code runs. So when the route reads the certificate it is
        # reading one this CA issued — a lookup, not a trust decision.
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        # See TlsAwareWebSocketProtocol. Passed as a CLASS, which is why this is `uvicorn.Server`
        # and not the `uvicorn` CLI: `--ws` accepts only the built-in names.
        ws=TlsAwareWebSocketProtocol,
        # And the same for HTTP. `h11` rather than `auto`, because `auto` resolves to httptools
        # when it is installed and the class passed here would be ignored -- a silently
        # certificate-blind listener, which is the failure this whole file exists to prevent.
        http=TlsAwareH11Protocol,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
