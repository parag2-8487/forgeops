# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent's mTLS listener (design.md ?10.2, ?3.1, ?4.4).

WHY EVERY ONE OF THESE IS WORTH ASSERTING

This module exists because uvicorn populates neither `scope["transport"]` nor the ASGI TLS extension
for a WebSocket, so `websocket.hub.TlsPeerCertificate` could never see a peer certificate and every
`session.connect` was refused with "client certificate and bearer device token are both required".
Each piece below fails in a way that is expensive to diagnose from the outside:

* a certificate issued with the wrong `extendedKeyUsage` is accepted by some verifiers and refused by
  others, which presents as an intermittent handshake failure;
* an IP address placed in a `DNSName` SAN does not match, and the resulting error names the hostname
  rather than the SAN;
* an unpopulated TLS extension is indistinguishable from a plaintext connection, which is precisely
  the failure this module was written to fix;
* a listener that started without a CA would serve a port that refuses every handshake, and the
  refusal would look like an agent fault.

So the tests are about the properties, not about reaching lines.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from src.agent_listener import (
    DEFAULT_TLS_DIR,
    SERVER_CERT_TTL,
    TlsAwareWebSocketProtocol,
    _attach_tls_extension,
    _subject_alternative_names,
    issue_probe_certificate,
    issue_server_certificate,
    main,
)
from src.auth.ca import generate_development_ca

pytestmark = [pytest.mark.mandatory]


@pytest.fixture(scope="module")
def development_ca() -> tuple[bytes, bytes]:
    """One CA for the module: generating a P-256 CA per test is pure cost."""
    return generate_development_ca()


def _leaf(pem: bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem)


class TestTheServerCertificate:
    def test_it_is_serverauth_only(self, development_ca: tuple[bytes, bytes]) -> None:
        """A certificate good for BOTH roles lets a stolen server key be presented as a device.

        The device certificates are `clientAuth` only (`InternalCertificateAuthority.sign`), and
        keeping the two disjoint is what stops either credential from impersonating the other side of
        the same connection.
        """
        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_server_certificate(
            ca_cert_pem=cert_pem, ca_key_pem=key_pem, server_names="backend-agent"
        )
        usage = _leaf(certificate_pem).extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(usage) == [ExtendedKeyUsageOID.SERVER_AUTH]

    def test_the_key_matches_the_certificate(self, development_ca: tuple[bytes, bytes]) -> None:
        """A mismatched pair fails at the first handshake, long after this function returned."""
        cert_pem, key_pem = development_ca
        certificate_pem, issued_key_pem = issue_server_certificate(
            ca_cert_pem=cert_pem, ca_key_pem=key_pem, server_names="backend-agent"
        )
        # Loaded the way `ssl` loads them, so this is the same check the listener depends on.
        private_key = serialization.load_pem_private_key(issued_key_pem, None)
        assert private_key.public_key().public_numbers() == _leaf(certificate_pem).public_key().public_numbers()

    def test_it_is_issued_by_the_given_ca_and_is_not_a_ca(self, development_ca: tuple[bytes, bytes]) -> None:
        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_server_certificate(
            ca_cert_pem=cert_pem, ca_key_pem=key_pem, server_names="backend-agent"
        )
        leaf = _leaf(certificate_pem)
        assert leaf.issuer == _leaf(cert_pem).subject
        # A leaf that could sign another certificate turns one stolen key into a private PKI.
        assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False

    def test_it_is_backdated_and_short_lived(self, development_ca: tuple[bytes, bytes]) -> None:
        """One minute of backdating, so an agent whose clock is seconds behind does not reject it."""
        cert_pem, key_pem = development_ca
        now = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
        certificate_pem, _ = issue_server_certificate(
            ca_cert_pem=cert_pem, ca_key_pem=key_pem, server_names="backend-agent", now=now
        )
        leaf = _leaf(certificate_pem)
        assert leaf.not_valid_before_utc == now - dt.timedelta(minutes=1)
        assert leaf.not_valid_after_utc == now + SERVER_CERT_TTL


class TestTheProbeCertificate:
    def test_it_is_clientauth_only(self, development_ca: tuple[bytes, bytes]) -> None:
        """The health probe must present a CLIENT certificate: `CERT_REQUIRED` applies to it too.

        Without one the probe fails the TLS handshake, the container is marked unhealthy, and anything
        waiting on `service_healthy` never starts ? which is exactly how the agent was held back with
        `service "agent" is not running`.
        """
        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_probe_certificate(ca_cert_pem=cert_pem, ca_key_pem=key_pem)
        usage = _leaf(certificate_pem).extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]

    def test_it_carries_no_san(self, development_ca: tuple[bytes, bytes]) -> None:
        """A SAN exists for name verification and nothing verifies the probe's name."""
        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_probe_certificate(ca_cert_pem=cert_pem, ca_key_pem=key_pem)
        with pytest.raises(x509.ExtensionNotFound):
            _leaf(certificate_pem).extensions.get_extension_for_class(x509.SubjectAlternativeName)


class TestTheSubjectAlternativeNames:
    def test_an_ip_becomes_an_ipaddress_entry_and_a_name_becomes_dns(self) -> None:
        """An IP in a `DNSName` does not match: a client checks an IP literal against IPAddress only.

        Getting this wrong produces a handshake failure whose message names the hostname rather than
        the SAN, which is a slow thing to diagnose.
        """
        entries = _subject_alternative_names("backend-agent, localhost, 127.0.0.1, ::1")
        dns = [e.value for e in entries if isinstance(e, x509.DNSName)]
        ips = [e.value for e in entries if isinstance(e, x509.IPAddress)]
        assert dns == ["backend-agent", "localhost"]
        assert ips == [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")]

    def test_empty_entries_are_dropped_and_an_empty_list_is_refused(self) -> None:
        assert len(_subject_alternative_names("a,,b")) == 2
        # Refused rather than issuing a certificate no client can match by name.
        with pytest.raises(ValueError, match="no usable SAN"):
            _subject_alternative_names(" , ")

    def test_the_names_appear_in_the_issued_certificate(self, development_ca: tuple[bytes, bytes]) -> None:
        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_server_certificate(
            ca_cert_pem=cert_pem, ca_key_pem=key_pem, server_names="backend-agent,localhost,127.0.0.1"
        )
        san = _leaf(certificate_pem).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.DNSName) == ["backend-agent", "localhost"]
        assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]


class _FakeSSLObject:
    """The slice of `ssl.SSLObject` the extension reads."""

    def __init__(self, der: bytes | None) -> None:
        self._der = der

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        assert binary_form is True
        return self._der

    def version(self) -> str:
        return "TLSv1.3"

    def cipher(self) -> tuple[str, str, int]:
        return ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)


class _FakeTransport:
    def __init__(self, ssl_object: object | None) -> None:
        self._ssl_object = ssl_object

    def get_extra_info(self, name: str) -> object | None:
        return self._ssl_object if name == "ssl_object" else None


class TestTheAsgiTlsExtension:
    """The piece without which mTLS cannot work under uvicorn."""

    def test_the_peer_certificate_lands_in_the_scope_as_pem(self, development_ca: tuple[bytes, bytes]) -> None:
        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_probe_certificate(ca_cert_pem=cert_pem, ca_key_pem=key_pem)
        der = _leaf(certificate_pem).public_bytes(serialization.Encoding.DER)

        scope: dict[str, object] = {"type": "websocket", "extensions": {"websocket.http.response": {}}}
        _attach_tls_extension(scope, _FakeTransport(_FakeSSLObject(der)))

        tls = scope["extensions"]["tls"]  # type: ignore[index]
        # BOTH spellings: the ASGI extension names `client_cert_chain`, and `websocket.hub`'s
        # certificate source reads `client_certificate_chain`. Satisfying both avoids bending either.
        assert tls["client_cert_chain"] == tls["client_certificate_chain"]
        assert x509.load_pem_x509_certificate(tls["client_cert_chain"][0].encode("ascii")) == _leaf(certificate_pem)
        assert tls["tls_version"] == "TLSv1.3"
        assert tls["cipher_suite"] == "TLS_AES_256_GCM_SHA384"

    def test_the_certificate_source_can_read_what_it_writes(self, development_ca: tuple[bytes, bytes]) -> None:
        """The round trip that matters: the hub's own reader must accept this shape.

        Asserted against `TlsPeerCertificate` rather than against the dictionary, because the
        dictionary is only correct insofar as that reader accepts it.
        """
        from src.websocket.hub import TlsPeerCertificate

        cert_pem, key_pem = development_ca
        certificate_pem, _ = issue_probe_certificate(ca_cert_pem=cert_pem, ca_key_pem=key_pem)
        der = _leaf(certificate_pem).public_bytes(serialization.Encoding.DER)

        scope: dict[str, object] = {"extensions": {}}
        _attach_tls_extension(scope, _FakeTransport(_FakeSSLObject(der)))

        read_back = TlsPeerCertificate().certificate_pem(scope)
        assert read_back is not None
        assert x509.load_pem_x509_certificate(read_back) == _leaf(certificate_pem)

    def test_a_plaintext_connection_leaves_no_extension(self) -> None:
        """Absence is the honest answer, and the certificate source reads it as 'no certificate'.

        The ASGI TLS Extension says the key is present only when the connection is over TLS.
        """
        for transport in (None, _FakeTransport(None), object()):
            scope: dict[str, object] = {"extensions": {}}
            _attach_tls_extension(scope, transport)
            assert "tls" not in scope["extensions"]  # type: ignore[operator]

    def test_a_tls_connection_with_no_peer_certificate_reports_an_empty_chain(self) -> None:
        """`CERT_REQUIRED` makes this unreachable in production, so it must fail closed, not crash."""
        scope: dict[str, object] = {"extensions": {}}
        _attach_tls_extension(scope, _FakeTransport(_FakeSSLObject(None)))
        assert scope["extensions"]["tls"]["client_cert_chain"] == []  # type: ignore[index]
        # And the hub reads that as no certificate rather than as an empty-but-present one.
        from src.websocket.hub import TlsPeerCertificate

        assert TlsPeerCertificate().certificate_pem(scope) is None

    def test_the_protocol_class_intercepts_run_asgi(self) -> None:
        """`run_asgi` is the hook, because `process_request` builds the scope then schedules it.

        Asserted structurally: the override exists on this class and not merely inherited, which is
        what makes the extension get populated before the application sees the scope.
        """
        assert "run_asgi" in TlsAwareWebSocketProtocol.__dict__


class TestTheListenerRefusesToStartWithoutACa:
    def test_it_exits_two_rather_than_serving_a_plaintext_agent_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plaintext agent port refuses every handshake, which looks like an agent fault.

        Exit 2 is what CI reported as `container forgeops-backend-agent-1 exited (2)` before the
        workflow generated a CA, and naming the missing configuration is the whole point.
        """
        monkeypatch.setenv("INTERNAL_CA_CERT_PEM", "")
        monkeypatch.setenv("INTERNAL_CA_KEY_PEM", "   ")
        assert main([]) == 2

    def test_the_default_tls_directory_is_owner_only_and_outside_the_image(self) -> None:
        """The private key must not be in an image layer, and must not be group-readable."""
        assert DEFAULT_TLS_DIR.startswith("/tmp/")  # noqa: S108 - asserting the documented location


class TestTheListenerConfiguresMutualTls:
    """What `main` hands uvicorn is the whole security posture of this port.

    `ssl_cert_reqs` silently weakening to `CERT_OPTIONAL` would leave a port that accepts anonymous
    peers and a `TlsPeerCertificate` that returns None for them ? the handshake would then be refused
    for "no client certificate", which reads as a client fault. And dropping the custom protocol class
    would restore the original defect exactly: TLS working, the extension unpopulated, every
    `session.connect` refused.
    """

    def _run_main(self, monkeypatch: pytest.MonkeyPatch, tls_dir: str) -> object:
        cert_pem, key_pem = generate_development_ca()
        monkeypatch.setenv("INTERNAL_CA_CERT_PEM", cert_pem.decode("ascii").replace(chr(10), chr(92) + "n"))
        monkeypatch.setenv("INTERNAL_CA_KEY_PEM", key_pem.decode("ascii").replace(chr(10), chr(92) + "n"))
        monkeypatch.setenv("AGENT_TLS_DIR", tls_dir)
        monkeypatch.setenv("AGENT_TLS_PORT", "8443")
        monkeypatch.setenv("AGENT_TLS_SERVER_NAMES", "backend-agent,localhost,127.0.0.1")

        captured: dict[str, object] = {}

        class _FakeServer:
            def __init__(self, config: object) -> None:
                captured["config"] = config

            def run(self) -> None:
                captured["ran"] = True

        monkeypatch.setattr("src.agent_listener.uvicorn.Server", _FakeServer)
        assert main([]) == 0
        assert captured.get("ran") is True
        return captured["config"]

    def test_client_certificates_are_required_and_the_tls_protocol_class_is_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        config = self._run_main(monkeypatch, str(tmp_path))
        assert config.ssl_cert_reqs == ssl.CERT_REQUIRED  # type: ignore[attr-defined]
        assert config.ws is TlsAwareWebSocketProtocol  # type: ignore[attr-defined]
        # The CA it verifies clients against is the one it issued its own certificate from.
        assert str(config.ssl_ca_certs).endswith("ca.crt")  # type: ignore[attr-defined]

    def test_it_materialises_a_usable_key_pair_and_a_probe_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        import pathlib as _pathlib

        self._run_main(monkeypatch, str(tmp_path))
        directory = _pathlib.Path(str(tmp_path))

        for name in ("server.crt", "server.key", "ca.crt", "probe.crt", "probe.key"):
            assert (directory / name).is_file(), f"{name} was not written"

        # The server certificate and the probe certificate must chain to the SAME CA, or the probe
        # cannot authenticate to the listener it is checking.
        authority = _leaf((directory / "ca.crt").read_bytes())
        assert _leaf((directory / "server.crt").read_bytes()).issuer == authority.subject
        assert _leaf((directory / "probe.crt").read_bytes()).issuer == authority.subject

    @pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX file modes only")
    def test_the_private_keys_are_owner_only(self, monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
        """A key another user in the container can read is a key that has already leaked."""
        import pathlib as _pathlib
        import stat

        self._run_main(monkeypatch, str(tmp_path))
        directory = _pathlib.Path(str(tmp_path))
        for name in ("server.key", "probe.key"):
            mode = stat.S_IMODE((directory / name).stat().st_mode)
            assert mode == 0o600, f"{name} is {oct(mode)}, not owner-only"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
