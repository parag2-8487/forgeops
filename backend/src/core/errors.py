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
    # D-53: an IdP outage is not a credential failure. §6.3 keeps Authentik out of
    # `/health/ready` so an outage "degrades login, not readiness" — which only means
    # anything if login answers something a client can act on. 503 says retry with
    # backoff and keep the session; 401 would say discard the credential and
    # re-authenticate through the provider that is down.
    "idp-unavailable": ProblemSpec(503, "Identity provider unavailable"),
    "forbidden": ProblemSpec(403, "Forbidden"),
    # D-56: the same distinction as `idp-unavailable`, one layer along. A Cerbos outage
    # is not a policy decision, and `forbidden` is byte-identical to a real deny by
    # design — so reporting an outage as 403 makes a dead authorisation layer look like
    # a working one refusing everyone, which is unfalsifiable from the client side.
    # Failing closed is preserved: nothing is allowed when Cerbos is silent.
    "authorization-unavailable": ProblemSpec(503, "Authorization service unavailable"),
    # ─── Pairing and devices (§1.1) ──────────────────────────────────────────
    "pairing-code-invalid": ProblemSpec(401, "Pairing code invalid"),
    "pairing-rate-limited": ProblemSpec(429, "Too many pairing attempts"),
    # D-71, three entries beyond Appendix C.1's table, each for a state the exchange can
    # reach and C.1 registered nothing for. The pattern is D-53's and D-56's: a state
    # that has no type gets reported as whatever is nearest, and "nearest" is always a
    # lie a client cannot detect.
    #
    # `pairing-unavailable` is the sharpest of the three. Both halves of the exchange are
    # Redis calls — the two §14.6 rate-limit buckets and the single-use consume script —
    # so a Redis outage refuses the exchange whatever happens. Without this entry that
    # refusal arrives as an unhandled 500 (a bug) or, worse, as 429 (a lie: 429 tells the
    # client to slow down, when in fact no rate was measured at all).
    "pairing-unavailable": ProblemSpec(503, "Pairing service unavailable"),
    # A submitted CSR that does not parse, whose self-signature does not verify, or whose
    # key is not EC P-256 (§3.1). Distinguishable from `pairing-code-invalid` on purpose:
    # the check runs BEFORE the code is consumed, so this answer reveals nothing about
    # whether the code exists, and folding it into the 401 would leave a broken agent
    # unable to tell a client bug from a wrong code.
    "csr-invalid": ProblemSpec(400, "Certificate request invalid"),
    # Revocation of a device id that does not exist. A 404 rather than the non-disclosing
    # 403 body, because the route is admin-only and an admin may read every device — §4.2's
    # enumeration rule constrains the `forbidden` body, not an admin-scoped 404.
    "device-not-found": ProblemSpec(404, "Device not found"),
    "device-revoked": ProblemSpec(401, "Device revoked"),
    "device-not-connected": ProblemSpec(409, "No agent connected"),
    "agent-timeout": ProblemSpec(504, "Agent operation timed out"),
    "agent-error": ProblemSpec(502, "Agent operation failed"),
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
    # ─── Repository import (§1.1, FR-01) ─────────────────────────────────────
    # Two types rather than one, because the two failures need different operator actions and a
    # single "import failed" would conflate them. Unconfigured is 503: the server cannot do this
    # yet and no retry by the caller will change that. Refused is 502: the request reached GitHub
    # and GitHub declined, so the caller's installation or permissions are what to look at.
    #
    # Registered here rather than raised ad hoc precisely because this set is closed — the previous
    # code's answer to "no credential" was to fabricate a token, which produced a 200.
    "repository-import-unconfigured": ProblemSpec(503, "Repository import not configured"),
    "repository-import-failed": ProblemSpec(502, "Repository import failed"),
    # ─── GitHub account link (Part 2) ────────────────────────────────────────
    #
    # THREE TYPES, NOT ONE, because they have three different remedies and an operator acts on the
    # remedy. Unconfigured is 503 and names the two settings to set — it is the state of every fresh
    # install, so it must read as "finish the setup" rather than as a fault. Absent is 409: the server
    # is configured and working, and the caller has simply not connected an account yet, which is the
    # normal state before the first connect and not a 404 (the endpoint exists) nor a 403 (nothing is
    # being hidden). Refused is 502, on the same reasoning as the import above: the request reached
    # GitHub and GitHub declined.
    "github-link-unconfigured": ProblemSpec(503, "GitHub account linking not configured"),
    "github-link-absent": ProblemSpec(409, "No GitHub account linked"),
    "github-link-failed": ProblemSpec(502, "GitHub refused the request"),
    # §2.1's two. `environment-absent` is a 404 rather than a 403 because an environment is not a
    # hidden resource — the caller is already authorised for the project that owns it, so concealing
    # which environments exist would buy nothing and cost every operator a confusing message.
    "environment-absent": ProblemSpec(404, "No such environment"),
    "environment-invalid": ProblemSpec(422, "The environment request is not valid"),
    # §2.2's three. `deployment-conflict` is a 409 rather than a 422 because the request was well
    # formed and the ROW had moved on — command delivery is at-least-once, so a second report for a
    # settled deployment is an expected event that must be reported as ignored rather than as an error.
    "deployment-absent": ProblemSpec(404, "No such deployment"),
    "deployment-invalid": ProblemSpec(422, "The deployment request is not valid"),
    "deployment-conflict": ProblemSpec(409, "The deployment has already settled"),
    # 2.2. A 503 because the request is well formed and would ordinarily be accepted: the service is
    # declining to attempt it after repeated validation failures. A 422 would blame the manifests in
    # THIS request, which is not what happened.
    "deployment-circuit-open": ProblemSpec(503, "Deployments to this environment are stopped"),
    # 2.2 and 2.4a. A 503 rather than a 500: the deployment is configured without a durable engine, so
    # the capability is absent rather than broken, and the detail names the setting that enables it.
    "pipeline-engine-absent": ProblemSpec(503, "No durable execution engine is configured"),
    # A 422: the request named a step that is not gated. Releasing it would do nothing while appearing
    # to succeed, which for an approval mechanism is the worst available outcome.
    "pipeline-step-not-gated": ProblemSpec(422, "That pipeline step has no approval gate"),
    # 2.7's three. `argocd-webhook-unconfigured` is a 503 because the capability is absent rather than
    # broken: with no secret, no payload can be authenticated, so the endpoint accepts NOTHING rather
    # than accepting anything, and the detail names the setting.
    "argocd-webhook-unconfigured": ProblemSpec(503, "No ArgoCD webhook secret is configured"),
    # A 422: the payload is well formed JSON that names no repository, so there is nothing to record.
    # Recording it anyway would put an entry in the history that matches no Application.
    "argocd-webhook-unrecognised": ProblemSpec(422, "The webhook payload names no repository"),
    # A 422: the render request omits a field its kind requires, which is a caller error and not a
    # server one -- an ApplicationSet with no environments generates no Applications.
    "argocd-manifest-incomplete": ProblemSpec(422, "The manifest request is missing a required field"),
    # 2.10's three. `monitoring-query-unknown` is a 422 rather than a 404 because the caller named a
    # query that does not exist in a fixed catalogue -- the request is malformed, not the resource
    # missing. `monitoring-query-forbidden` is a 403: the query is real and covers the whole
    # deployment, which is operator information.
    "monitoring-query-unknown": ProblemSpec(422, "No such monitoring query"),
    "monitoring-query-forbidden": ProblemSpec(403, "That monitoring query is operator-only"),
    "monitoring-query-invalid": ProblemSpec(422, "The monitoring query arguments are not valid"),
    # 2.11's four. `incident-suggestion-already-submitted` is a 409 rather than a 422: the request is
    # well formed and the conflict is with state, and two change sets proposing the same edit would both
    # pass approval while the second conflicted with the first -- presenting to an operator as an
    # unexplained refusal of their own change.
    "incident-absent": ProblemSpec(404, "No such incident"),
    "incident-suggestion-absent": ProblemSpec(404, "No such fix suggestion for this incident"),
    "incident-suggestion-already-submitted": ProblemSpec(409, "That suggestion is already a change set"),
    "incident-governance-absent": ProblemSpec(503, "Governance is not composed, so a fix cannot be proposed"),
    # 2.12's three. `healing-requires-approval` is a 403 and not a 409: the caller asked for something
    # they are not permitted to have done automatically, and the answer does not change by retrying.
    "healing-refused": ProblemSpec(409, "A self-healing guard rail refused this action"),
    "healing-requires-approval": ProblemSpec(403, "That remedy cannot be executed without an approval"),
    "healing-governance-absent": ProblemSpec(503, "Governance is not composed, so no healing can occur"),
    # 2.13's five. All 422 or 404: this domain mutates only its own bookkeeping, so there is no
    # governance refusal to express and no state conflict -- a bad verdict or scope is a malformed
    # request, and a missing preference is a missing resource.
    "learning-verdict-invalid": ProblemSpec(422, "That feedback verdict is not valid"),
    "learning-scope-invalid": ProblemSpec(422, "That preference scope is not valid"),
    "learning-preference-invalid": ProblemSpec(422, "The preference change is not valid"),
    "learning-preference-absent": ProblemSpec(404, "No such preference"),
    "learning-turn-invalid": ProblemSpec(422, "That conversation turn is not valid"),
    # 2.5's two. Both 422: an utterance the command set cannot express, or a plan missing a required
    # slot, is a malformed request rather than a forbidden one -- there is no authority question here,
    # because the command does not exist to be authorised for.
    "command-refused": ProblemSpec(422, "That command is not one this system understands"),
    "command-incomplete": ProblemSpec(422, "That command is missing a required value"),
    # 2.14's one. A 404, and the SAME sentence for an absent project and for one in another tenant --
    # non-disclosing, so this cannot be used to probe which project ids exist elsewhere.
    "knowledge-refused": ProblemSpec(404, "No such project, or that topic is not answerable"),
    # 2.6's two. `notification-absent` is a 404 for the same reason `environment-absent` is: the caller is
    # already authorised for the project that owns it. `notification-preference-invalid` is a 422 and is
    # raised for one specific case worth naming -- an enabled channel with no target, which would be a
    # setting that silently does nothing.
    "notification-absent": ProblemSpec(404, "No such notification"),
    "notification-preference-invalid": ProblemSpec(422, "The notification preference is not valid"),
    # ─── Tenancy (§6.7) ──────────────────────────────────────────────────────
    "tenant-context-missing": ProblemSpec(500, "Tenant context missing"),
    # ─── Direct Export & Deployment ──────────────────────────────────────────
    "github-push-failed": ProblemSpec(502, "Failed to push to GitHub"),
    "github-create-failed": ProblemSpec(502, "Failed to create GitHub repository"),
    "github-init-failed": ProblemSpec(502, "Failed to initialize GitHub repository"),
    "github-blob-upload-failed": ProblemSpec(502, "Failed to upload file to GitHub"),
    "github-tree-failed": ProblemSpec(502, "Failed to create GitHub tree"),
    "github-commit-failed": ProblemSpec(502, "Failed to create GitHub commit"),
    "github-ref-failed": ProblemSpec(502, "Failed to update GitHub reference"),
    "project-empty": ProblemSpec(400, "Project has no indexed files"),
    "validation-error": ProblemSpec(422, "Request validation error"),
    "vercel-deploy-failed": ProblemSpec(502, "Failed to deploy to Vercel"),
    "idempotency-conflict": ProblemSpec(409, "Idempotency conflict"),
    "autonomous-run-conflict": ProblemSpec(409, "Autonomous deployment run state conflict"),
    "github-repository-not-found": ProblemSpec(404, "Repository not found or inaccessible"),
    "github-permission-denied": ProblemSpec(403, "Linked account lacks required permissions"),
    "github-rate-limited": ProblemSpec(429, "GitHub API rate limit exceeded"),
    "github-upstream-unreachable": ProblemSpec(502, "Could not connect to GitHub API"),
    "github-upstream-invalid": ProblemSpec(502, "GitHub API returned an invalid response structure"),
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

        # THE DETAIL NAMES THE FIELD. It said only "One or more fields failed validation.", which is
        # a sentence that sends the reader to look at their whole request. A `max_length` rejection on
        # a generation prompt then surfaced to a user as a vague health-sounding error, when the
        # actual complaint — "String should have at most 4000 characters" — was sitting unused in
        # `errors` the whole time. The generic sentence is kept only for the case where `errors` is
        # empty and there is genuinely nothing more specific to say.
        detail = "One or more fields failed validation."
        if errors_list:
            first = errors_list[0]
            field = first["pointer"].rsplit("/", 1)[-1] or "request"
            detail = f"{field}: {first['detail']}"
            if len(errors_list) > 1:
                detail += f" (and {len(errors_list) - 1} more)"

        return _render(
            request,
            ProblemDetail(
                type=f"{TYPE_BASE}/validation-failed",
                title="Request validation failed",
                status=422,
                detail=detail,
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
