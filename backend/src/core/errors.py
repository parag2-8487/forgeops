# SPDX-License-Identifier: FSL-1.1-ALv2
"""RFC 9457 Problem Details error rendering (design.md §4.2, §11.2).

Every non-2xx response carries application/problem+json with status matching HTTP
status, instance = request path, trace_id, and detail that never leaks secrets.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Final, NamedTuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from .logging import trace_id_var

PROBLEM_CONTENT_TYPE = "application/problem+json"
TYPE_BASE = "https://errors.forgeops.dev"

logger = logging.getLogger(__name__)

# Patterns to scrub from error details before they reach clients
_LEAK_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"postgresql(?:\+\w+)?://[^\s]+"),
    re.compile(r"redis://[^\s]+"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9]{20,}"),
    re.compile(r"xai-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE|PUBLIC)\s+KEY-----"),
    re.compile(r"Traceback \(most recent call last\)"),
]


def _sanitize_detail(detail: str | None) -> str | None:
    """Remove secrets and tracebacks from detail text before it reaches clients."""
    if detail is None:
        return None
    for pattern in _LEAK_PATTERNS:
        if pattern.search(detail):
            return None  # If any leak pattern matches, suppress the detail entirely
    return detail


def _slugify(text: str) -> str:
    """Convert a phrase to a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class ProblemSpec(NamedTuple):
    """One registered problem type: a stable `type` suffix and its FIXED status."""

    status: int
    title: str


#: The Phase 1 problem-type registry (design.md Appendix C.1).
#:
#: Phase 0 passed `type_suffix` and `status` as independent arguments at every raise
#: site, so the same suffix could be raised as 401 in one place and 403 in another and
#: nothing would object. A client keying off `type` — which is the whole point of RFC
#: 9457, since `type` is stable and `title` is not — would then see the same type carry
#: different semantics. The registry makes the pairing single-sourced, and
#: `ProblemException` refuses a status that disagrees with it.
#:
#: `type` is stable and NEVER resolved at runtime; `status` always equals the HTTP
#: status (P-09); `detail` never carries secrets, tokens, connection strings or
#: tracebacks (D-27, Q-24), which `_sanitize_detail` enforces rather than assumes.
#:
#: Two entries are deliberately not error statuses, and are registered anyway so the
#: body shape is uniform: `approval-required` (202) and `iteration-bound-exhausted`
#: (200) both carry a real payload.
PROBLEM_REGISTRY: Final[dict[str, ProblemSpec]] = {
    # ─── Authentication and authorization (§1.11) ────────────────────────────
    "unauthenticated": ProblemSpec(401, "Unauthenticated"),
    "forbidden": ProblemSpec(403, "Forbidden"),
    # ─── Pairing and devices (§1.1) ──────────────────────────────────────────
    "pairing-code-invalid": ProblemSpec(401, "Pairing code invalid"),
    "pairing-rate-limited": ProblemSpec(429, "Too many pairing attempts"),
    "device-revoked": ProblemSpec(401, "Device revoked"),
    "device-not-connected": ProblemSpec(409, "No agent connected"),
    # ─── Command envelopes (§7.6) ────────────────────────────────────────────
    "envelope-signature-invalid": ProblemSpec(401, "Envelope signature invalid"),
    "envelope-replayed": ProblemSpec(409, "Envelope replayed"),
    "envelope-expired": ProblemSpec(401, "Envelope expired"),
    "envelope-unsupported-version": ProblemSpec(400, "Unsupported envelope version"),
    "operation-unknown": ProblemSpec(400, "Unknown operation"),
    # ─── Policy (§1.7) ───────────────────────────────────────────────────────
    "policy-denied": ProblemSpec(403, "Policy denied"),
    "policy-bundle-stale": ProblemSpec(409, "Policy bundle stale"),
    "governance-policy-undefined": ProblemSpec(503, "Governance policy undefined"),
    # ─── Approval and change sets (§1.6) ─────────────────────────────────────
    "approval-required": ProblemSpec(202, "Approval required"),
    "approval-forbidden": ProblemSpec(403, "Approval forbidden"),
    "approval-expired": ProblemSpec(409, "Approval expired"),
    "blast-radius-blocked": ProblemSpec(409, "Blast radius blocked"),
    "change-set-conflict": ProblemSpec(409, "Change set conflict"),
    "change-set-already-applied": ProblemSpec(409, "Change set already applied"),
    "apply-rolled-back": ProblemSpec(500, "Apply rolled back"),
    "revert-unavailable": ProblemSpec(409, "Revert unavailable"),
    # ─── Generation (§1.5) ───────────────────────────────────────────────────
    "iteration-bound-exhausted": ProblemSpec(200, "Iteration bound exhausted"),
    "generation-unavailable": ProblemSpec(503, "Generation unavailable"),
    # ─── Secrets (§1.8) ──────────────────────────────────────────────────────
    "secret-redaction-failed": ProblemSpec(422, "Secret redaction failed"),
    "secret-store-unavailable": ProblemSpec(503, "Secret store unavailable"),
    # ─── Indexing and analysis (§1.3) ────────────────────────────────────────
    "project-embedding-backend-locked": ProblemSpec(409, "Embedding backend locked"),
    "index-version-conflict": ProblemSpec(409, "Index version conflict"),
    "scan-in-progress": ProblemSpec(409, "Scan already in progress"),
    # ─── Audit (§1.9) ────────────────────────────────────────────────────────
    # A failed audit write ABORTS the mutation: §1.9's guarantee is that every action
    # is logged, and an action that happened without a record would break Q-04 and,
    # worse, be invisible. Availability is traded for auditability, deliberately.
    "audit-write-failed": ProblemSpec(500, "Audit write failed"),
    # ─── Validation (§1.5) ───────────────────────────────────────────────────
    "dryrun-unavailable": ProblemSpec(503, "Dry run unavailable"),
    "validator-unavailable": ProblemSpec(503, "Validator unavailable"),
    # ─── Tenancy (§6.7) ──────────────────────────────────────────────────────
    "tenant-context-missing": ProblemSpec(500, "Tenant context missing"),
}

#: The 403 body, byte-identical for every forbidden outcome (design §4.2, Appendix
#: C.1, Q-20).
#:
#: A 403 that says "no such project" for an unknown id and "forbidden" for one the
#: caller may not see is an enumeration oracle: an attacker learns which project ids
#: exist by reading the difference. So the detail is a FIXED string, and
#: `forbidden_problem()` is the only way to build it — a caller cannot pass a detail
#: that reintroduces the distinction.
FORBIDDEN_DETAIL: Final[str] = "You do not have permission to perform this action."


def problem_spec(type_suffix: str) -> ProblemSpec | None:
    """The registered spec for a suffix, or None when it is not registered."""
    return PROBLEM_REGISTRY.get(type_suffix)


def problem(
    type_suffix: str,
    *,
    detail: str | None = None,
    errors: list[dict[str, str]] | None = None,
) -> ProblemException:
    """Build a registered problem, taking its status and title from the registry.

    Preferred over constructing `ProblemException` directly, because the status cannot
    be passed and therefore cannot disagree with the registry.
    """
    spec = PROBLEM_REGISTRY.get(type_suffix)
    if spec is None:
        raise KeyError(
            f"{type_suffix!r} is not a registered problem type. Add it to "
            f"PROBLEM_REGISTRY with its fixed status (design.md Appendix C.1) rather "
            f"than inventing a type at the raise site."
        )
    return ProblemException(status=spec.status, type_suffix=type_suffix, title=spec.title, detail=detail, errors=errors)


def forbidden_problem() -> ProblemException:
    """The non-disclosing 403 (design §4.2, Q-20).

    Takes no arguments on purpose. Every forbidden outcome must produce a
    byte-identical body, whether or not the resource exists, so there is nothing for a
    caller to vary.
    """
    spec = PROBLEM_REGISTRY["forbidden"]
    return ProblemException(
        status=spec.status,
        type_suffix="forbidden",
        title=spec.title,
        detail=FORBIDDEN_DETAIL,
    )


class ProblemDetail(BaseModel):
    """RFC 9457 Problem Details response body."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    trace_id: str | None = None
    errors: list[dict[str, str]] | None = None


class ProblemException(Exception):
    """Raise to produce an RFC 9457 response with the given problem shape."""

    def __init__(
        self,
        *,
        status: int,
        type_suffix: str,
        title: str,
        detail: str | None = None,
        errors: list[dict[str, str]] | None = None,
    ):
        # A registered type carries a FIXED status (Appendix C.1). Raising the same
        # suffix as 401 in one place and 403 in another makes `type` — the one member
        # RFC 9457 promises is stable — mean two different things to a client. Checked
        # here rather than only in `problem()`, because a direct construction is
        # exactly the path that would bypass the registry.
        spec = PROBLEM_REGISTRY.get(type_suffix)
        if spec is not None and spec.status != status:
            raise ValueError(
                f"problem type {type_suffix!r} is registered with status "
                f"{spec.status}, not {status}. Use core.errors.problem({type_suffix!r}) "
                f"or correct PROBLEM_REGISTRY; a type must not carry two statuses."
            )
        self.problem = ProblemDetail(
            type=f"{TYPE_BASE}/{type_suffix}",
            title=title,
            status=status,
            detail=detail,
            errors=errors,
        )
        super().__init__(title)


def _render(request: Request, problem: ProblemDetail) -> Response:
    """Render a ProblemDetail as an application/problem+json response."""
    problem.instance = request.url.path
    problem.trace_id = trace_id_var.get("") or None
    # Ensure detail is sanitized
    problem.detail = _sanitize_detail(problem.detail)
    return Response(
        content=problem.model_dump_json(exclude_none=True),
        status_code=problem.status,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def install_problem_handlers(app: FastAPI) -> None:
    """Install RFC 9457 exception handlers for all error types."""

    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException) -> Response:
        return _render(request, exc.problem)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors_list = []
        for e in exc.errors():
            loc_parts = [str(p) for p in e["loc"][1:]] if len(e["loc"]) > 1 else [str(p) for p in e["loc"]]
            pointer = "#/" + "/".join(loc_parts) if loc_parts else "#/"
            errors_list.append({"pointer": pointer, "detail": e["msg"]})
        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/validation-failed",
                title="Request validation failed",
                status=422,
                detail="One or more fields failed validation.",
                errors=errors_list,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        status_code = exc.status_code
        try:
            phrase = HTTPStatus(status_code).phrase
        except ValueError:
            phrase = "Unknown Error"
        detail_text = exc.detail if isinstance(exc.detail, str) else None
        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/{_slugify(phrase)}",
                title=phrase,
                status=status_code,
                detail=_sanitize_detail(detail_text),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        logger.exception("unhandled exception")
        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/internal",
                title="Internal Server Error",
                status=500,
                detail="An unexpected error occurred. Quote the trace_id when reporting this.",
            ),
        )
