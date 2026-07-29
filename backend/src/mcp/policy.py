# SPDX-License-Identifier: FSL-1.1-ALv2
"""OPA gateway policy client — blast-radius tool filtering and call authorisation.

Design §11.4 and §5.4. Two behaviours are deliberately different, and the
difference is the whole point of this module:

* **OPA unreachable** (connection refused, timeout, 5xx) — fail *closed and
  quietly*: `filter_tools` returns an empty list and `authorise_call` raises 403.
  A policy engine that fails open is not a policy engine.

* **Policy document undefined** (OPA is healthy but the queried rule does not
  exist) — fail *closed and loudly*: raise a 503 problem. OPA answers an
  undefined document with HTTP 200 and a body that simply has no ``result`` key,
  so `raise_for_status()` never fires. Treating that as "deny everything" makes a
  mis-deployed or renamed policy indistinguishable from a working one that denies,
  which is exactly how a policy bundle can go missing unnoticed.

The queried paths must name real rules in ``policies/mcp/gateway.rego``
(``package mcp.gateway`` → ``filter`` and ``allow``). ``tests/unit/test_mcp_policy.py``
asserts the path/rule agreement so a rename cannot drift silently.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..core.errors import ProblemException

logger = logging.getLogger(__name__)

# `package mcp.gateway` in policies/mcp/gateway.rego → /v1/data/mcp/gateway/<rule>
DEFAULT_FILTER_PATH = "/v1/data/mcp/gateway/filter"
DEFAULT_ALLOW_PATH = "/v1/data/mcp/gateway/allow"


class OpaUnavailableError(RuntimeError):
    """OPA could not be reached or returned a transport-level failure."""


class OpaPolicyUndefinedError(RuntimeError):
    """OPA responded, but the queried policy document is undefined."""


def _subject(claims: Any) -> str:
    """Extract the verified subject from decoded claims.

    `OidcTokenVerifier.verify` returns the decoded claim mapping, and `sub` is
    required for the rate-limiter key (design §15.2), so it is always present by
    the time policy runs. `getattr` covers a dataclass-shaped Claims object.
    """
    if isinstance(claims, dict):
        return str(claims.get("sub", ""))
    return str(getattr(claims, "sub", ""))


class OpaGatewayPolicy:
    """Blast-radius tool filtering and call authorisation via OPA.

    - `filter_tools` runs on EVERY tools/list response, cache hit included.
    - `authorise_call` raises before any upstream dispatch (P-05).
    """

    def __init__(
        self,
        *,
        opa_url: str,
        filter_path: str = DEFAULT_FILTER_PATH,
        allow_path: str = DEFAULT_ALLOW_PATH,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._opa_url = opa_url.rstrip("/")
        self._filter_path = filter_path
        self._allow_path = allow_path
        self._http = http or httpx.AsyncClient(timeout=5.0)

    # ── Public contract (design §11.4) ────────────────────────────────────────

    async def filter_tools(
        self,
        *,
        server: str,
        tools: list[dict[str, Any]],
        claims: Any,
        blast_radius: str,
    ) -> list[dict[str, Any]]:
        """Return the subset of `tools` the caller may see.

        Fail-closed on an unreachable OPA (empty list). Raises on an undefined
        policy document.
        """
        opa_input = {
            "server": server,
            "tools": tools,
            "tool": None,
            "subject": _subject(claims),
            "agent_blast_radius": blast_radius,
        }
        try:
            result = await self._query(self._filter_path, opa_input)
        except OpaUnavailableError:
            logger.warning("OPA unavailable for filter_tools; fail-closed to an empty tool list")
            return []

        if not isinstance(result, list):
            raise ProblemException(
                status=503,
                type_suffix="mcp-policy-malformed",
                title="Policy result malformed",
                detail="The gateway filter policy did not return a list of tools.",
            )
        return result

    async def authorise_call(
        self,
        *,
        server: str,
        tool: str,
        metadata: dict[str, Any],
        claims: Any,
        blast_radius: str,
    ) -> None:
        """Authorise one tools/call. Raises 403 on deny, 503 on undefined policy."""
        opa_input = {
            "server": server,
            "tool": tool,
            "tools": [metadata.get("tool_descriptor") or {"name": tool, **_annotations(metadata)}],
            "metadata": metadata,
            "subject": _subject(claims),
            "agent_blast_radius": blast_radius,
        }
        try:
            result = await self._query(self._allow_path, opa_input)
        except OpaUnavailableError:
            logger.warning("OPA unavailable for authorise_call; fail-closed to deny")
            result = False

        if result is not True:
            raise ProblemException(
                status=403,
                type_suffix="mcp-call-denied",
                title="Tool call denied",
                detail=f"Policy denied calling '{tool}' on server '{server}'.",
            )

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _query(self, path: str, opa_input: dict[str, Any]) -> Any:
        """POST an input document to OPA and return the `result` value.

        Raises `OpaUnavailableError` when OPA cannot be reached, and
        `ProblemException` (503) when OPA answers but the document is undefined.
        """
        try:
            resp = await self._http.post(f"{self._opa_url}{path}", json={"input": opa_input})
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # transport, status, or non-JSON body
            raise OpaUnavailableError(str(exc)) from exc

        if not isinstance(body, dict) or "result" not in body:
            # OPA returns 200 {} for an undefined document. Surfacing this as a
            # deny would hide a missing or renamed policy bundle forever.
            logger.error("OPA policy document undefined at %s", path)
            raise ProblemException(
                status=503,
                type_suffix="mcp-policy-undefined",
                title="Policy document undefined",
                detail=f"The gateway policy at '{path}' is not defined in OPA.",
            )
        return body["result"]


def _annotations(metadata: dict[str, Any]) -> dict[str, Any]:
    """Carry a resolved tool's blast-radius annotation into the OPA input.

    The Rego `allow` rule reads `input.tools[_].annotations.blast_radius` and
    defaults an unannotated tool to `infrastructure` — the highest radius — so a
    tool whose metadata is unknown is denied to anything less privileged.
    """
    annotations = metadata.get("annotations")
    return {"annotations": annotations} if isinstance(annotations, dict) else {}
