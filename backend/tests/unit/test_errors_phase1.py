# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Phase 1 RFC 9457 registry (design.md §4.2, §11.2, Appendix C.1).

Three things are asserted, and the first is why this file exists at all:

1. **The registry matches Appendix C.1, parsed from the design document.** A test that
   restated the 33 suffixes and statuses here would be a second copy of the authority,
   and the two would drift. Reading the table means adding a row to the design without
   registering it is a failing test rather than a discovered gap.

2. **The 403 is non-disclosing.** A 403 that says "no such project" for an unknown id
   and "forbidden" for one the caller may not see is an enumeration oracle. The body
   must be byte-identical either way.

3. **No `detail` can carry a secret.** D-27 repaired this for tracebacks; Q-24 extends
   it. Every registered type is driven through the real handler with a synthetic
   credential in its detail, so a new type cannot arrive without the protection.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.errors import (
    FORBIDDEN_DETAIL,
    PROBLEM_CONTENT_TYPE,
    PROBLEM_REGISTRY,
    TYPE_BASE,
    ProblemException,
    forbidden_problem,
    install_problem_handlers,
    problem,
)

from tests import synthetic_secrets

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN = REPO_ROOT / ".antigravity" / "specs" / "phase-1-mvp-core" / "design.md"

#: An Appendix C.1 row: `| `suffix` | 401 | when | detail guidance |`
_C1_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|\s*(\d{3})\s*\|")


def appendix_c1_types() -> dict[str, int]:
    """`{suffix: status}` parsed from Appendix C.1's table.

    Bounded to the C.1 subsection so C.2's agent error-code table — which uses the
    same vocabulary but has no HTTP status column — cannot leak in.
    """
    lines = DESIGN.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("### C.1"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("### C.2"))

    found: dict[str, int] = {}
    for line in lines[start:end]:
        match = _C1_ROW.match(line)
        if match:
            found[match.group(1)] = int(match.group(2))
    return found


C1_TYPES = appendix_c1_types()


def _app_raising(exc: ProblemException) -> TestClient:
    """A real app whose route raises `exc`, so the registered handler renders it."""
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


class TestTheRegistryMatchesTheAuthority:
    def test_the_appendix_was_actually_parsed(self) -> None:
        """A parser that silently matched nothing would make every test below vacuous."""
        assert len(C1_TYPES) >= 30, C1_TYPES

    @pytest.mark.parametrize("suffix", sorted(C1_TYPES), ids=sorted(C1_TYPES))
    def test_every_appendix_type_is_registered(self, suffix: str) -> None:
        assert suffix in PROBLEM_REGISTRY, f"{suffix} is in Appendix C.1 but not in PROBLEM_REGISTRY"

    @pytest.mark.parametrize("suffix", sorted(C1_TYPES), ids=sorted(C1_TYPES))
    def test_every_registered_status_matches_the_appendix(self, suffix: str) -> None:
        assert PROBLEM_REGISTRY[suffix].status == C1_TYPES[suffix]

    def test_the_registry_adds_nothing_the_appendix_does_not_define(self) -> None:
        """An unlisted type is a vocabulary a client cannot look up."""
        extra = sorted(set(PROBLEM_REGISTRY) - set(C1_TYPES))
        assert not extra, extra

    def test_every_type_uri_is_stable_and_absolute(self) -> None:
        for suffix in PROBLEM_REGISTRY:
            uri = f"{TYPE_BASE}/{suffix}"
            assert uri.startswith("https://errors.forgeops.dev/")
            assert " " not in uri


class TestStatusCannotDriftFromTheRegistry:
    def test_a_disagreeing_status_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="registered with status 403"):
            ProblemException(status=401, type_suffix="forbidden", title="Forbidden")

    def test_the_factory_needs_no_status_at_all(self) -> None:
        assert problem("index-version-conflict").problem.status == 409

    def test_an_unregistered_suffix_cannot_be_raised_through_the_factory(self) -> None:
        with pytest.raises(KeyError, match="not a registered problem type"):
            problem("invented-at-the-raise-site")

    def test_an_unregistered_suffix_is_still_allowed_directly(self) -> None:
        """Phase 0 call sites pass free-form suffixes; they must keep working.

        The registry constrains types it KNOWS about rather than forbidding everything
        else, so adopting it did not require rewriting every Phase 0 raise site in the
        same change.
        """
        exc = ProblemException(status=418, type_suffix="mcp-invalid-token", title="x")
        assert exc.problem.status == 418

    @pytest.mark.parametrize("suffix", sorted(PROBLEM_REGISTRY), ids=sorted(PROBLEM_REGISTRY))
    def test_the_rendered_status_equals_the_body_status(self, suffix: str) -> None:
        """P-09: `status` in the body always equals the HTTP status."""
        client = _app_raising(problem(suffix, detail="a harmless detail"))
        response = client.get("/boom")
        assert response.status_code == PROBLEM_REGISTRY[suffix].status
        assert response.json()["status"] == response.status_code


class TestTheForbiddenBodyIsNonDisclosing:
    def test_the_body_is_byte_identical_for_a_missing_and_a_denied_resource(self) -> None:
        app = FastAPI()
        install_problem_handlers(app)

        @app.get("/resource/{name}")
        async def resource(name: str) -> None:
            # Whether the resource exists or not, the SAME object is raised. That is
            # the structural version of the guarantee: there is no branch that could
            # produce a different body.
            raise forbidden_problem()

        client = TestClient(app, raise_server_exceptions=False)
        exists = client.get("/resource/a-project-that-exists")
        missing = client.get("/resource/a-project-that-does-not-exist")

        assert exists.status_code == missing.status_code == 403
        # `instance` legitimately differs (it is the request path), and `trace_id` is
        # per-request. Every other member must be identical, so nothing distinguishes
        # the two outcomes.
        left = {k: v for k, v in exists.json().items() if k not in {"instance", "trace_id"}}
        right = {k: v for k, v in missing.json().items() if k not in {"instance", "trace_id"}}
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

    def test_the_detail_is_the_fixed_string(self) -> None:
        assert forbidden_problem().problem.detail == FORBIDDEN_DETAIL

    def test_the_helper_accepts_no_caller_supplied_detail(self) -> None:
        """There must be nothing for a caller to vary."""
        import inspect

        assert not inspect.signature(forbidden_problem).parameters

    def test_the_fixed_detail_names_no_resource(self) -> None:
        lowered = FORBIDDEN_DETAIL.lower()
        for leak in ("project", "not found", "does not exist", "unknown", "id"):
            assert leak not in lowered, f"the fixed 403 detail hints at {leak!r}"


class TestNoDetailCanCarryASecret:
    """D-27 plus Q-24, applied to every registered type rather than a sample."""

    @pytest.mark.parametrize(
        "leaking",
        [
            pytest.param(synthetic_secrets.bearer_clause(), id="bearer"),
            pytest.param(synthetic_secrets.postgres_dsn(), id="postgres-dsn"),
            pytest.param(synthetic_secrets.redis_dsn(), id="redis-dsn"),
            pytest.param(synthetic_secrets.openai_style_key(), id="provider-key"),
            pytest.param(synthetic_secrets.pem_header(), id="pem-header"),
            pytest.param("Traceback (most recent call last):\n  File x", id="traceback"),
        ],
    )
    def test_a_leaking_detail_is_suppressed_for_every_type(self, leaking: str) -> None:
        for suffix in PROBLEM_REGISTRY:
            client = _app_raising(problem(suffix, detail=f"context: {leaking}"))
            body = client.get("/boom").json()
            assert body.get("detail") is None, f"{suffix} rendered a leaking detail: {body}"
            assert synthetic_secrets.SYNTHETIC_MARKER not in json.dumps(body), suffix

    def test_a_clean_detail_still_survives(self) -> None:
        """Suppression must not be indiscriminate, or the guarantee is worthless.

        If every detail vanished, the test above would pass while the problem documents
        became useless — the shape of the P-09 defect the review found.
        """
        client = _app_raising(problem("index-version-conflict", detail="current version is 7"))
        assert client.get("/boom").json()["detail"] == "current version is 7"

    def test_the_response_media_type_is_problem_json(self) -> None:
        client = _app_raising(problem("policy-denied", detail="rule denied"))
        assert client.get("/boom").headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
