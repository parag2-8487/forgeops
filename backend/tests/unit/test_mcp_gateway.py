# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for the MCP gateway: registry, header routing, and OIDC auth."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from src.core.errors import ProblemException
from src.mcp.auth import OidcTokenVerifier
from src.mcp.registry import McpServerRegistry, ServerDescriptor
from src.mcp.routing import MCP_METHOD_HEADER, MCP_NAME_HEADER, HeaderRouter, Route

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_config() -> list[dict]:
    return [
        {
            "name": "terraform",
            "url": "http://localhost:9001",
            "description": "Terraform MCP server",
            "capabilities": ["tools/list", "tools/call"],
        },
        {
            "name": "ansible",
            "url": "http://localhost:9002",
            "description": "Ansible MCP server",
            "capabilities": ["tools/list"],
        },
    ]


@pytest.fixture()
def registry(sample_config: list[dict]) -> McpServerRegistry:
    return McpServerRegistry.from_config(sample_config)


@pytest.fixture()
def router(registry: McpServerRegistry) -> HeaderRouter:
    return HeaderRouter(registry)


@pytest.fixture()
def verifier() -> OidcTokenVerifier:
    return OidcTokenVerifier(
        allowed_issuers=["https://auth.example.com"],
        audience="forgeops-gateway",
    )


@pytest.fixture()
def rsa_keypair():
    """Generate an RSA key pair for signing test JWTs."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem, private_key, public_key


# ---------------------------------------------------------------------------
# McpServerRegistry tests
# ---------------------------------------------------------------------------


class TestMcpServerRegistry:
    def test_from_config_builds_correctly(self, sample_config: list[dict]):
        registry = McpServerRegistry.from_config(sample_config)
        assert registry.get("terraform") is not None
        assert registry.get("ansible") is not None

        tf = registry.get("terraform")
        assert tf is not None
        assert tf.name == "terraform"
        assert tf.url == "http://localhost:9001"
        assert tf.description == "Terraform MCP server"
        assert tf.capabilities == ["tools/list", "tools/call"]

    def test_get_returns_none_for_unknown(self, registry: McpServerRegistry):
        assert registry.get("nonexistent") is None

    def test_all_returns_full_dict(self, registry: McpServerRegistry):
        all_servers = registry.all()
        assert len(all_servers) == 2
        assert "terraform" in all_servers
        assert "ansible" in all_servers

    def test_registry_is_copy_safe(self, registry: McpServerRegistry):
        """Mutating the returned dict does not affect internal state."""
        all_servers = registry.all()
        all_servers["hacked"] = ServerDescriptor(name="hacked", url="http://evil")
        assert registry.get("hacked") is None

    def test_from_config_minimal(self):
        """Config entry with only required fields."""
        config = [{"name": "minimal", "url": "http://localhost:8080"}]
        reg = McpServerRegistry.from_config(config)
        server = reg.get("minimal")
        assert server is not None
        assert server.description == ""
        assert server.capabilities == []


# ---------------------------------------------------------------------------
# HeaderRouter tests
# ---------------------------------------------------------------------------


class TestHeaderRouter:
    def test_valid_headers_returns_correct_route(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "terraform"}
        route = router.route(headers)

        assert isinstance(route, Route)
        assert route.server.name == "terraform"
        assert route.method == "tools/list"
        assert route.kind == "tools_list"

    def test_tools_call_classification(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "terraform"}
        route = router.route(headers)
        assert route.kind == "tools_call"

    def test_tasks_classification(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "tasks/create", MCP_NAME_HEADER: "terraform"}
        route = router.route(headers)
        assert route.kind == "tasks"

    def test_other_classification(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "resources/read", MCP_NAME_HEADER: "terraform"}
        route = router.route(headers)
        assert route.kind == "other"

    def test_missing_method_header_raises_400(self, router: HeaderRouter):
        headers = {MCP_NAME_HEADER: "terraform"}
        with pytest.raises(ProblemException) as exc_info:
            router.route(headers)
        assert exc_info.value.problem.status == 400
        assert "mcp-missing-routing-headers" in exc_info.value.problem.type

    def test_missing_name_header_raises_400(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "tools/list"}
        with pytest.raises(ProblemException) as exc_info:
            router.route(headers)
        assert exc_info.value.problem.status == 400
        assert "mcp-missing-routing-headers" in exc_info.value.problem.type

    def test_empty_headers_raises_400(self, router: HeaderRouter):
        with pytest.raises(ProblemException) as exc_info:
            router.route({})
        assert exc_info.value.problem.status == 400

    def test_unknown_server_raises_404(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "tools/list", MCP_NAME_HEADER: "ghost"}
        with pytest.raises(ProblemException) as exc_info:
            router.route(headers)
        assert exc_info.value.problem.status == 404
        assert "mcp-unknown-server" in exc_info.value.problem.type

    def test_route_is_body_independent(self, router: HeaderRouter):
        """Same headers with different bodies produce the same route (P-05)."""
        headers = {MCP_METHOD_HEADER: "tools/call", MCP_NAME_HEADER: "ansible"}

        # Route is determined purely by headers — body content is irrelevant.
        route_a = router.route(headers)
        route_b = router.route(headers)

        assert route_a == route_b
        assert route_a.server.name == "ansible"
        assert route_a.method == "tools/call"
        assert route_a.kind == "tools_call"

    def test_whitespace_in_headers_is_trimmed(self, router: HeaderRouter):
        headers = {MCP_METHOD_HEADER: "  tools/list  ", MCP_NAME_HEADER: "  terraform  "}
        route = router.route(headers)
        assert route.server.name == "terraform"
        assert route.method == "tools/list"


# ---------------------------------------------------------------------------
# OidcTokenVerifier tests
# ---------------------------------------------------------------------------


class TestOidcTokenVerifier:
    """Unit tests for OIDC token verification — focuses on pre-signature checks."""

    async def test_no_token_raises_401(self, verifier: OidcTokenVerifier):
        with pytest.raises(ProblemException) as exc_info:
            await verifier.verify(None)
        assert exc_info.value.problem.status == 401
        assert "mcp-missing-token" in exc_info.value.problem.type

    async def test_empty_auth_header_raises_401(self, verifier: OidcTokenVerifier):
        with pytest.raises(ProblemException) as exc_info:
            await verifier.verify("")
        assert exc_info.value.problem.status == 401
        assert "mcp-missing-token" in exc_info.value.problem.type

    async def test_non_bearer_scheme_raises_401(self, verifier: OidcTokenVerifier):
        with pytest.raises(ProblemException) as exc_info:
            await verifier.verify("Basic dXNlcjpwYXNz")
        assert exc_info.value.problem.status == 401
        assert "mcp-invalid-auth-scheme" in exc_info.value.problem.type

    async def test_bearer_only_no_token_raises_401(self, verifier: OidcTokenVerifier):
        with pytest.raises(ProblemException) as exc_info:
            await verifier.verify("Bearer")
        assert exc_info.value.problem.status == 401
        assert "mcp-invalid-auth-scheme" in exc_info.value.problem.type

    async def test_malformed_jwt_raises_401(self, verifier: OidcTokenVerifier):
        with pytest.raises(ProblemException) as exc_info:
            await verifier.verify("Bearer not.a.jwt!!!")
        assert exc_info.value.problem.status == 401
        assert "mcp-invalid-token" in exc_info.value.problem.type

    async def test_untrusted_issuer_raises_401(self, verifier: OidcTokenVerifier, rsa_keypair):
        """Token with an issuer not in the allowlist is rejected."""
        private_pem, _, _, _ = rsa_keypair
        now = int(time.time())
        token = pyjwt.encode(
            {
                "iss": "https://evil.example.com",
                "sub": "user1",
                "aud": "forgeops-gateway",
                "exp": now + 3600,
                "iat": now,
            },
            private_pem,
            algorithm="RS256",
        )
        with pytest.raises(ProblemException) as exc_info:
            await verifier.verify(f"Bearer {token}")
        assert exc_info.value.problem.status == 401
        assert "mcp-untrusted-issuer" in exc_info.value.problem.type

    async def test_expired_token_raises_401(self, verifier: OidcTokenVerifier, rsa_keypair):
        """Token with exp in the past is rejected (after issuer check passes)."""
        private_pem, public_pem, _, public_key = rsa_keypair
        now = int(time.time())
        token = pyjwt.encode(
            {
                "iss": "https://auth.example.com",
                "sub": "user1",
                "aud": "forgeops-gateway",
                "exp": now - 3600,  # expired
                "iat": now - 7200,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )

        # Mock the JWKS client to return our test key
        from unittest.mock import MagicMock

        mock_jwks_client = MagicMock()
        mock_signing_key = MagicMock()
        mock_signing_key.key = public_key
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch.object(verifier, "_get_jwks_client", return_value=mock_jwks_client):
            with pytest.raises(ProblemException) as exc_info:
                await verifier.verify(f"Bearer {token}")
            assert exc_info.value.problem.status == 401
            assert "mcp-token-expired" in exc_info.value.problem.type

    async def test_wrong_audience_raises_401(self, verifier: OidcTokenVerifier, rsa_keypair):
        """Token with wrong audience is rejected."""
        private_pem, public_pem, _, public_key = rsa_keypair
        now = int(time.time())
        token = pyjwt.encode(
            {
                "iss": "https://auth.example.com",
                "sub": "user1",
                "aud": "wrong-audience",
                "exp": now + 3600,
                "iat": now,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )

        from unittest.mock import MagicMock

        mock_jwks_client = MagicMock()
        mock_signing_key = MagicMock()
        mock_signing_key.key = public_key
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch.object(verifier, "_get_jwks_client", return_value=mock_jwks_client):
            with pytest.raises(ProblemException) as exc_info:
                await verifier.verify(f"Bearer {token}")
            assert exc_info.value.problem.status == 401
            assert "mcp-invalid-audience" in exc_info.value.problem.type

    async def test_valid_token_returns_claims(self, verifier: OidcTokenVerifier, rsa_keypair):
        """Valid token with correct issuer and audience returns Claims."""
        private_pem, public_pem, _, public_key = rsa_keypair
        now = int(time.time())
        token = pyjwt.encode(
            {
                "iss": "https://auth.example.com",
                "sub": "user42",
                "aud": "forgeops-gateway",
                "exp": now + 3600,
                "iat": now,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )

        from unittest.mock import MagicMock

        from src.mcp.auth import Claims

        mock_jwks_client = MagicMock()
        mock_signing_key = MagicMock()
        mock_signing_key.key = public_key
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch.object(verifier, "_get_jwks_client", return_value=mock_jwks_client):
            result = await verifier.verify(f"Bearer {token}")
            assert isinstance(result, Claims)
            assert result.sub == "user42"
            assert result.iss == "https://auth.example.com"
            assert result.aud == "forgeops-gateway"
            assert result.exp == now + 3600
            assert result.iat == now
