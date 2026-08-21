# SPDX-License-Identifier: FSL-1.1-ALv2
"""The authorization-code + PKCE flow, over `httpx` + `pyjwt` (design.md §3.5, §11.2).

No new auth library
-------------------
§16.2 records the decision not to add `authlib`: the flow below is the whole of what
Phase 1 needs — one authorization URL, one token exchange, one refresh — and an auth
library is a large surface for a small need. Everything here is `httpx` for transport
and `pyjwt` for verification, both already pinned.

What is deliberately *not* hand-rolled is verification. `IdTokenVerifier` extends
Phase 0's `OidcTokenVerifier` (now in `core.security`, §11.2) rather than decoding the
ID token itself, so the exact-issuer allowlist, the JWKS cache, the asymmetric-only
algorithm list and the required-claims set are the same code the product API uses. Two
copies of a token verifier is two places for a verification bug to live.

The audience distinction, stated once
-------------------------------------
Three audiences exist and they are not interchangeable:

* the **ID token** is audienced to `OIDC_CLIENT_ID` — it is a statement to *this
  client* about who logged in;
* the **access token** is audienced to `OIDC_APP_AUDIENCE` and is what
  `AppTokenVerifier` checks on every product route;
* the MCP gateway's audience is a third value, unchanged by Phase 1.

Verifying an ID token with the app audience, or vice versa, would accept a token minted
for a different purpose. They are therefore separate verifier instances, constructed
with different audiences, and never shared.

`nbf`
----
§3.5 lists `nbf` among the checked claims. It is enforced when present rather than
required, because OIDC Core makes `nbf` optional in an ID Token — requiring it would
reject a spec-compliant IdP at login, which is a self-inflicted outage rather than a
security gain. `sub` *is* required, because a token that verifies without a subject
cannot be resolved to a user and must not be resolved to a default one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import jwt

from ..core.errors import ProblemException, problem
from ..core.security import Claims, OidcTokenVerifier
from .models import UserRole

#: The scopes Phase 1 asks for. `openid` is mandatory; `profile` and `email` populate
#: `users.name` and `users.email`; `offline_access` is what makes a refresh token
#: available, and without it `/refresh` would have nothing to present.
#: The scope whose Authentik property mapping carries the claims THIS API requires.
#:
#: `AppTokenVerifier` refuses a token without a `forgeops_role` string, and checks `aud` against the
#: product API's audience -- which §7.1 deliberately makes distinct from the OIDC client id. Both
#: claims are emitted by a provider scope mapping, and Authentik only evaluates a mapping when its
#: scope is REQUESTED. Omitting it therefore produced a token that authenticated perfectly at the IdP
#: and was refused by every route here, which reads as a broken login rather than a missing scope.
FORGEOPS_SCOPE: Final[str] = "forgeops"

DEFAULT_SCOPES: Final[tuple[str, ...]] = (
    "openid",
    "profile",
    "email",
    "offline_access",
    FORGEOPS_SCOPE,
)

#: How long an in-flight authorization request may sit in Redis before its PKCE
#: verifier is discarded. Five minutes is generous for a human login and short enough
#: that a leaked `state` is useless by the time it is replayed.
PKCE_STATE_TTL_SECONDS: Final[int] = 300

#: Redis key prefix for the single-use PKCE/state record.
PKCE_STATE_KEY_PREFIX: Final[str] = "forgeops:oidc:state:"

#: IdP group name → role. Authentik carries group membership; §11.2 requires the
#: callback to map it to **exactly one** of the three roles.
#:
#: Both the namespaced Authentik group names and the bare role names are accepted, so a
#: deployment that names its groups after the roles needs no extra configuration. The
#: mapping is data rather than a chain of `if`s so Q-20 can enumerate it.
GROUP_ROLE_MAP: Final[dict[str, UserRole]] = {
    "forgeops-admins": UserRole.ADMIN,
    "forgeops-developers": UserRole.DEVELOPER,
    "forgeops-viewers": UserRole.VIEWER,
    "admin": UserRole.ADMIN,
    "developer": UserRole.DEVELOPER,
    "viewer": UserRole.VIEWER,
}

#: Role precedence, widest last. A user in two groups gets the wider role, which is the
#: only defensible reading of "member of admins and developers".
_ROLE_PRECEDENCE: Final[tuple[UserRole, ...]] = (UserRole.VIEWER, UserRole.DEVELOPER, UserRole.ADMIN)


def role_from_groups(groups: object) -> UserRole:
    """Map IdP groups to exactly one role, defaulting to the narrowest.

    A caller whose groups are absent, empty, or entirely unrecognised becomes a
    `viewer`. That is the one safe default: `viewer` can mutate nothing, so a
    misconfigured group mapping degrades to read-only access rather than to no access
    at all (which would lock every user out of a working IdP) or to write access
    (which would grant authority the IdP never asserted).

    Accepts `object` because the value comes from a token claim and may be any JSON
    type. A non-list claim is treated as no groups rather than raising: the token is
    already cryptographically verified, so the failure is a mapping problem, and
    failing the login would hide it behind a 401 that looks like bad credentials.
    """
    if isinstance(groups, str):
        candidates: list[str] = [groups]
    elif isinstance(groups, list | tuple | set | frozenset):
        candidates = [str(g) for g in groups]
    else:
        candidates = []

    resolved = {GROUP_ROLE_MAP[name] for name in (c.strip().lower() for c in candidates) if name in GROUP_ROLE_MAP}
    if not resolved:
        return UserRole.VIEWER
    return max(resolved, key=_ROLE_PRECEDENCE.index)


def _b64url(raw: bytes) -> str:
    """Base64url without padding, per RFC 7636 §4.2."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class PkceChallenge:
    """An RFC 7636 code verifier and its S256 challenge.

    `plain` is not supported and there is no parameter to request it. RFC 7636 §7.2
    permits `plain` only where S256 is impossible, which is never true here, and a
    `method` parameter would be a switch an attacker-influenced request could try to
    flip.
    """

    verifier: str
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls) -> PkceChallenge:
        # 32 random bytes → 43 base64url characters, the RFC's minimum length and its
        # recommended entropy.
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        return cls(verifier=verifier, challenge=challenge)


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Everything the login endpoint produced: where to send the browser, and the
    secrets that must survive until the callback."""

    url: str
    state: str
    nonce: str
    verifier: str


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """The server-side half of an in-flight login.

    Held in Redis under the `state`, never in a cookie. A cookie-held verifier would
    be readable by anything that can read cookies for the origin, which defeats the
    point of PKCE binding the exchange to the client that started it.
    """

    verifier: str
    nonce: str
    next_path: str

    def to_json(self) -> str:
        return json.dumps({"verifier": self.verifier, "nonce": self.nonce, "next": self.next_path})

    @classmethod
    def from_json(cls, raw: str | bytes) -> PendingLogin:
        data = json.loads(raw)
        return cls(
            verifier=str(data["verifier"]),
            nonce=str(data["nonce"]),
            next_path=str(data.get("next") or "/"),
        )


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """A token endpoint response, with only the members Phase 1 uses."""

    access_token: str
    id_token: str
    refresh_token: str | None
    expires_in: int
    token_type: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TokenResponse:
        access = payload.get("access_token")
        id_token = payload.get("id_token")
        if not isinstance(access, str) or not access or not isinstance(id_token, str) or not id_token:
            # A 200 from the token endpoint that omits either token is a broken IdP,
            # not a bad caller. It still resolves to `unauthenticated`, because the
            # caller has no credential either way and there is nothing they can fix by
            # learning more.
            raise problem("unauthenticated")
        refresh = payload.get("refresh_token")
        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        return cls(
            access_token=access,
            id_token=id_token,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_in=expires_in,
            token_type=str(payload.get("token_type") or "bearer"),
        )


@dataclass(frozen=True, slots=True)
class OidcMetadata:
    """The three endpoints Phase 1 needs from the discovery document."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str

    @classmethod
    def from_document(cls, issuer: str, document: dict[str, Any]) -> OidcMetadata:
        declared = str(document.get("issuer") or "")
        # The discovery document's own `issuer` must equal the configured one. A
        # document that claims a different issuer is either misconfigured or an
        # attacker's — and honouring its endpoints would send the authorization code
        # somewhere the configuration never named.
        if declared.rstrip("/") != issuer.rstrip("/"):
            raise problem(
                "idp-unavailable",
                detail="The OIDC discovery document declares a different issuer.",
            )
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(document.get(key), str) or not document[key]:
                raise problem(
                    "idp-unavailable",
                    detail=f"The OIDC discovery document is missing {key}.",
                )
        return cls(
            issuer=declared,
            authorization_endpoint=str(document["authorization_endpoint"]),
            token_endpoint=str(document["token_endpoint"]),
            jwks_uri=str(document["jwks_uri"]),
        )


class IdTokenVerifier(OidcTokenVerifier):
    """Verifies an ID token: audience is the CLIENT ID, not the app audience.

    Reports every failure as the single registered `unauthenticated` type, for the same
    reason `AppTokenVerifier` does: telling a caller which check failed tells them what
    to change, and at a login endpoint the caller who benefits most from that is the
    one guessing.
    """

    problem_types = dict.fromkeys(  # noqa: RUF012 - overrides a class attribute by design
        ("missing", "scheme", "undecodable", "issuer", "expired", "audience", "signature"),
        "unauthenticated",
    )

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        jwks_ttl_seconds: int = 600,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            allowed_issuers=[issuer] if issuer else [],
            audience=client_id,
            jwks_ttl_seconds=jwks_ttl_seconds,
            http=http,
        )

    def _reject(self, mode: str, title: str, detail: str) -> ProblemException:
        del title, detail  # see the class docstring
        return problem(self.problem_types[mode])

    async def verify_id_token(self, id_token: str, *, nonce: str | None = None) -> Claims:
        """Verify an ID token and bind it to the nonce this login started with.

        The nonce check is what stops an ID token captured from one login being
        replayed into another: it is minted by us, held server-side, and never leaves
        the Redis record until the callback reads it.
        """
        claims = await self.verify(f"Bearer {id_token}")
        if not claims.sub:
            raise problem("unauthenticated")
        if nonce is not None and str(claims.raw.get("nonce") or "") != nonce:
            raise problem("unauthenticated")
        return claims


class OidcClient:
    """The authorization-code + PKCE client (§3.5).

    Holds no per-request state: the PKCE verifier and nonce travel through
    `PkceStateStore`, so two logins in flight for one browser cannot overwrite each
    other's secrets — which a module-level dict would allow.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_url: str,
        http: httpx.AsyncClient,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        public_base_url: str = "",
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_url = redirect_url
        self._http = http
        self._scopes = scopes
        #: Where a BROWSER can reach the IdP, when that differs from where this process reaches it.
        #:
        #: Empty means they are the same, which is the single-host case and the default. It is not the
        #: containerised case: inside Compose this process resolves `authentik-server:9000` and a
        #: browser on the host cannot, while the host's `localhost:9000` is not reachable from inside a
        #: container -- and the ports are bound to 127.0.0.1 deliberately, so widening them is not an
        #: option. There is therefore NO single host:port both sides can use, and pretending otherwise
        #: is what produced a login that redirected a real browser to an unresolvable name.
        #:
        #: Only the AUTHORIZATION endpoint is rewritten. Discovery, token and JWKS are server-to-server
        #: and must keep using the internal issuer -- and because Authentik derives the `iss` claim from
        #: the request that mints the token, keeping the token call internal is also what keeps `iss`
        #: equal to the configured issuer.
        self._public_base_url = public_base_url.rstrip("/")
        self._metadata: OidcMetadata | None = None

    def _browser_reachable(self, endpoint: str) -> str:
        """Rewrite an endpoint's origin to the browser-reachable one, path and query intact."""
        if not self._public_base_url:
            return endpoint
        parsed = urlsplit(endpoint)
        public = urlsplit(self._public_base_url)
        return urlunsplit((public.scheme, public.netloc, parsed.path, parsed.query, parsed.fragment))

    @property
    def client_id(self) -> str:
        return self._client_id

    async def metadata(self) -> OidcMetadata:
        """Fetch and cache the discovery document.

        Cached for the process lifetime rather than with a TTL: the three endpoint URLs
        of an issuer do not rotate, and a TTL would add a failure mode (a refetch
        during an IdP blip turning a working login into a 503) for no benefit. A
        genuinely moved endpoint is a configuration change, which restarts the process.
        """
        if self._metadata is not None:
            return self._metadata
        url = f"{self._issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise problem(
                "idp-unavailable",
                detail="The OIDC discovery document could not be read.",
            ) from exc
        if not isinstance(document, dict):
            raise problem(
                "idp-unavailable",
                detail="The OIDC discovery document is not an object.",
            )
        self._metadata = OidcMetadata.from_document(self._issuer, document)
        return self._metadata

    async def authorization_request(self, *, next_path: str = "/") -> tuple[AuthorizationRequest, PendingLogin]:
        """Build the redirect the browser follows, plus the record to hold server-side."""
        metadata = await self.metadata()
        pkce = PkceChallenge.generate()
        state = _b64url(secrets.token_bytes(32))
        nonce = _b64url(secrets.token_bytes(16))
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_url,
                "scope": " ".join(self._scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce.challenge,
                "code_challenge_method": pkce.method,
            }
        )
        # The one URL a BROWSER has to be able to open, so it is the one rewritten.
        authorize = self._browser_reachable(metadata.authorization_endpoint)
        separator = "&" if "?" in authorize else "?"
        return (
            AuthorizationRequest(
                url=f"{authorize}{separator}{query}",
                state=state,
                nonce=nonce,
                verifier=pkce.verifier,
            ),
            PendingLogin(verifier=pkce.verifier, nonce=nonce, next_path=next_path),
        )

    async def exchange_code(self, *, code: str, verifier: str) -> TokenResponse:
        """Exchange an authorization code, presenting the PKCE verifier."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_url,
                "client_id": self._client_id,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, *, refresh_token: str) -> TokenResponse:
        """Exchange a refresh token for a fresh access token."""
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "scope": " ".join(self._scopes),
            }
        )

    async def _token_request(self, form: dict[str, str]) -> TokenResponse:
        metadata = await self.metadata()
        payload = dict(form)
        if self._client_secret:
            # `client_secret_post`. Authentik accepts both this and Basic; the form
            # variant keeps the secret out of a header that proxies commonly log.
            payload["client_secret"] = self._client_secret
        try:
            response = await self._http.post(metadata.token_endpoint, data=payload)
        except httpx.HTTPError as exc:
            raise problem(
                "idp-unavailable",
                detail="The OIDC token endpoint could not be reached.",
            ) from exc

        if response.status_code >= 400:
            # Every token-endpoint rejection — bad code, expired code, wrong verifier,
            # revoked refresh token — is one `unauthenticated`. The IdP's own error
            # code is deliberately not forwarded: `invalid_grant` versus
            # `invalid_client` tells a caller which half of the exchange to attack.
            raise problem("unauthenticated")
        try:
            body = response.json()
        except ValueError as exc:
            raise problem("unauthenticated") from exc
        if not isinstance(body, dict):
            raise problem("unauthenticated")
        return TokenResponse.from_payload(body)


def access_token_expiry(token: str, fallback: int) -> int:
    """Seconds until the access token expires, read from the token when possible.

    The `expires_in` an IdP returns is advisory and some omit it. Reading `exp` from
    the token the client will actually present keeps the value the frontend schedules
    its refresh on tied to the credential rather than to a hint. Decoded WITHOUT
    signature verification and used only to compute a countdown — never to authorise
    anything, which is what makes that safe here.
    """
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except jwt.PyJWTError:
        return fallback
    exp = claims.get("exp")
    iat = claims.get("iat")
    if isinstance(exp, int) and isinstance(iat, int) and exp > iat:
        return exp - iat
    return fallback
