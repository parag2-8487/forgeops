# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-20 — RBAC and secret-value confinement (design.md §4.2, §11.2, §11.8, Appendix B).

Property, universally quantified over `(role, resource, action)` triples:

    A viewer is denied every mutating action; **no role** can read a secret *value*
    through any route; and a 403 body is byte-identical whether or not the resource
    exists.

Why this is a property and not the matrix test
----------------------------------------------
`test_cerbos_matrix.py` asserts §11.2's table cell by cell — a closed, transcribed list. It
cannot say anything about an action nobody transcribed, and the failure this property guards
against is precisely a *new* action arriving: a `read_value` added to a policy, a mutating
verb added to a resource, a viewer verb that was never in the table. So the triples here are
**generated** from the resource kinds and from a generated action vocabulary, including verbs
no policy mentions, and the claim is a universal one about shape rather than a list of cells.

The three clauses are asserted at the layers where each is enforceable:

* *viewer cannot mutate* — against the real Cerbos policy set, over generated mutating verbs;
* *no role reads a secret value* — twice, because it has two halves that can fail
  independently: the policy must deny it, and **no route may exist that could serve it**.
  A policy that denies an action no route offers is not the same guarantee as a route that
  cannot exist; §11.2's "the value exists to be injected at deploy time" needs both;
* *the 403 body is not an oracle* — over generated resource ids, existing and not.

Gating: `require_capability("cerbos")`. Skips locally without the sidecar, **fails** under
`FORGEOPS_REQUIRE_INTEGRATION=1`.

Negative control (`mutations.toml` Q-20): add `read_value` to the viewer's Cerbos policy.
The property must then fail.
"""

from __future__ import annotations

import os
import re
import uuid
from functools import lru_cache
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.requests import Request

from tests.integration.capability import require_capability

pytestmark = [pytest.mark.mandatory, pytest.mark.cerbos]

CERBOS_URL_ENV = "FORGEOPS_TEST_CERBOS_URL"

#: The viewer the enumeration-oracle probe authenticates as. Fixed, because the clause is
#: about the RESOURCE differing while everything else stays the same.
PROBE_VIEWER_ID = "33333333-3333-3333-3333-333333333333"

ROLES = ("admin", "developer", "viewer")

#: The six resource kinds §11.2 defines. Read from the policy directory rather than
#: restated, so a seventh resource cannot arrive untested.
POLICY_DIR_NAMES = ("project", "change_set", "policy", "secret", "agent_device", "audit")

#: Verbs that change state. `read`, `read_metadata` and `list` are the only non-mutating
#: verbs Phase 1 has, so anything else in the vocabulary below is mutating by construction —
#: including verbs no policy mentions, which must be denied by deny-by-default.
NON_MUTATING = frozenset({"read", "read_metadata", "list"})

#: Deliberately wider than §11.2's table. The interesting cases are the verbs nobody wrote a
#: rule for: an action a policy does not mention must be denied, not allowed because no rule
#: matched. Every value is a plausible English verb a future leaf might add.
ACTION_VOCABULARY = (
    "create",
    "read",
    "read_metadata",
    "read_value",
    "reveal",
    "update",
    "patch",
    "delete",
    "purge",
    "approve",
    "reject",
    "apply",
    "revert",
    "pair",
    "revoke",
    "rotate",
    "export",
    "import",
    "truncate",
    "grant",
    "impersonate",
    "exfiltrate",
)

#: Route paths or names that would expose a secret VALUE. Matched against the real router.
SECRET_VALUE_ROUTE_PATTERNS = (
    re.compile(r"/secrets?/[^/]*/(value|reveal|plaintext|decrypt)", re.IGNORECASE),
    re.compile(r"/secrets?/(value|reveal|plaintext|decrypt)", re.IGNORECASE),
)

#: Handler names that would do the same under a different path.
SECRET_VALUE_HANDLER_NAMES = ("read_secret_value", "reveal_secret", "secret_plaintext", "decrypt_secret")


def _cerbos_url() -> str:
    url = os.environ.get(CERBOS_URL_ENV, "").strip()
    if not url:
        require_capability(
            "cerbos",
            f"{CERBOS_URL_ENV} is not set; Q-20 asks the real policy set, because a "
            "reimplementation of Cerbos in the test would be asserting this file's opinion",
        )
    return url.rstrip("/")


@lru_cache(maxsize=1)
def cerbos_base_url() -> str:
    """Probed once, synchronously, so an unreachable sidecar is reported once."""
    import urllib.error
    import urllib.request

    from src.auth.cerbos import HEALTH_PATH

    url = _cerbos_url()
    try:
        with urllib.request.urlopen(f"{url}{HEALTH_PATH}", timeout=10) as response:  # noqa: S310
            assert response.status == 200, response.status
    except (urllib.error.URLError, OSError) as exc:
        pytest.fail(f"{CERBOS_URL_ENV}={url} but the sidecar did not answer {HEALTH_PATH}: {exc}")
    return url


def _principal(role: str, principal_id: str | None = None) -> Any:
    from src.auth.cerbos import CerbosPrincipal

    return CerbosPrincipal(
        id=principal_id or str(uuid.uuid4()),
        roles=(role,),
        attr={"kind": "user", "blast_radius": "workspace"},
    )


def _resource(kind: str, resource_id: str, *, owner: str | None = None) -> Any:
    from src.auth.cerbos import CerbosResource

    attr: dict[str, Any] = {}
    if owner is not None:
        attr = {"owner_id": owner, "member_ids": [owner], "created_by": owner}
    return CerbosResource(kind=kind, id=resource_id, attr=attr)


def _decide(url: str, *, principal: Any, resource: Any, action: str) -> bool:
    """One synchronous decision from the real sidecar.

    Synchronous because Hypothesis drives these bodies, and an `httpx.AsyncClient` created
    per example would bind its pool to a fresh event loop each time — the failure mode that
    made `test_cerbos_matrix.py` report `WriteTimeout` for half its cases before its fixture
    scope was corrected.
    """
    payload = {
        "requestId": uuid.uuid4().hex,
        "principal": principal.to_payload(),
        "resources": [{"resource": resource.to_payload(), "actions": [action]}],
    }
    response = httpx.post(f"{url}/api/check/resources", json=payload, timeout=10.0)
    assert response.status_code == 200, response.text[:300]
    for entry in response.json().get("results") or []:
        ref = entry.get("resource") or {}
        if (ref.get("kind"), ref.get("id")) == (resource.kind, resource.id):
            return entry.get("actions", {}).get(action) == "EFFECT_ALLOW"
    return False


_TRIPLE_SETTINGS = settings(
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class TestTheVocabularyIsNotVacuous:
    """Guards every clause below: a generator that produced nothing, or only non-mutating
    verbs, would make the viewer clause pass without asserting anything."""

    def test_the_action_vocabulary_contains_mutating_verbs(self) -> None:
        mutating = [action for action in ACTION_VOCABULARY if action not in NON_MUTATING]
        assert len(mutating) >= 15, mutating

    def test_the_vocabulary_is_wider_than_the_policy_set(self) -> None:
        """The point of generating: verbs nobody wrote a rule for must be denied too."""
        assert "exfiltrate" in ACTION_VOCABULARY
        assert "impersonate" in ACTION_VOCABULARY
        assert "reveal" in ACTION_VOCABULARY

    def test_every_resource_kind_has_a_policy_file(self) -> None:
        from pathlib import Path

        policy_dir = Path(__file__).resolve().parents[3] / "policies" / "cerbos"
        present = {path.stem for path in policy_dir.glob("*.yaml")}
        missing = [kind for kind in POLICY_DIR_NAMES if kind not in present]
        assert not missing, f"{missing} have no Cerbos policy, so their triples prove nothing"


class TestAViewerIsDeniedEveryMutatingAction:
    @_TRIPLE_SETTINGS
    @given(
        kind=st.sampled_from(POLICY_DIR_NAMES),
        action=st.sampled_from([a for a in ACTION_VOCABULARY if a not in NON_MUTATING]),
        owns=st.booleans(),
    )
    def test_the_answer_is_always_deny(self, kind: str, action: str, owns: bool) -> None:
        """`owns` matters: the derived roles list all three coarse roles as parents, so a
        viewer CAN be an owner or a member — which is what makes "a viewer mutates nothing"
        a claim about the policy rather than about the resource's attributes."""
        url = cerbos_base_url()
        viewer_id = str(uuid.uuid4())
        principal = _principal("viewer", viewer_id)
        resource = _resource(kind, f"{kind}-{uuid.uuid4().hex[:8]}", owner=viewer_id if owns else None)
        allowed = _decide(url, principal=principal, resource=resource, action=action)
        assert not allowed, (
            f"a viewer was allowed the mutating action {action!r} on {kind!r} "
            f"(owns={owns}); §11.2 gives a viewer read access and nothing else"
        )


class TestNoRoleCanReadASecretValue:
    """Two independent halves. Both are required, and neither implies the other."""

    @_TRIPLE_SETTINGS
    @given(
        role=st.sampled_from(ROLES),
        action=st.sampled_from(("read_value", "reveal", "decrypt", "plaintext", "export")),
        owns=st.booleans(),
    )
    def test_the_policy_denies_every_value_reading_verb_to_every_role(self, role: str, action: str, owns: bool) -> None:
        """Not even admin. §11.2: the value exists to be injected at deploy time, and a
        reveal endpoint would turn the vault into a distribution channel."""
        url = cerbos_base_url()
        principal_id = str(uuid.uuid4())
        principal = _principal(role, principal_id)
        resource = _resource("secret", f"secret-{uuid.uuid4().hex[:8]}", owner=principal_id if owns else None)
        allowed = _decide(url, principal=principal, resource=resource, action=action)
        assert not allowed, f"role {role!r} was allowed {action!r} on a secret (owns={owns})"

    def test_no_route_could_serve_a_secret_value(self) -> None:
        """The half a policy cannot give you.

        A policy denying an action no route offers is not the same guarantee as a route that
        cannot exist. This enumerates the real router, so it starts checking `/api/v1/secrets`
        the moment task 11.8 adds it — and it is deliberately pattern-based rather than an
        exact-path allowlist, because the failure mode is a *new* route.
        """
        from tests.property.test_q19_route_coverage import built_app, checker

        module = checker()
        offenders: list[str] = []
        examined = 0
        for prefix, route in module._flatten(built_app().routes):  # noqa: SLF001
            raw_path = getattr(route, "path", None)
            if raw_path is None:
                continue
            path = f"{prefix}{raw_path}"
            examined += 1
            for pattern in SECRET_VALUE_ROUTE_PATTERNS:
                if pattern.search(path):
                    offenders.append(path)
            endpoint = getattr(route, "endpoint", None)
            name = getattr(endpoint, "__name__", "")
            if name in SECRET_VALUE_HANDLER_NAMES:
                offenders.append(f"{path} -> {name}")
        assert examined > 0, "no routes examined; this assertion would be vacuous"
        assert not offenders, (
            f"routes that could serve a secret VALUE: {offenders}. §11.2 forbids the "
            "endpoint, not merely the permission — `read_metadata` returns key, "
            "environment, rotation date and last-updated only."
        )

    def test_the_route_scan_can_actually_detect_one(self) -> None:
        """The vacuity guard for the clause above: a pattern set that matched nothing would
        make "no route could serve a secret value" true of any router at all."""
        for candidate in (
            "/api/v1/secrets/{id}/value",
            "/api/v1/secrets/{id}/reveal",
            "/api/v1/secret/abc/plaintext",
        ):
            assert any(pattern.search(candidate) for pattern in SECRET_VALUE_ROUTE_PATTERNS), candidate
        assert not any(pattern.search("/api/v1/secrets/{id}") for pattern in SECRET_VALUE_ROUTE_PATTERNS)
        assert not any(pattern.search("/api/v1/secrets") for pattern in SECRET_VALUE_ROUTE_PATTERNS)


@lru_cache(maxsize=1)
def _probe_client() -> Any:
    return _build_probe_client()


def _build_probe_client() -> Any:
    """A real FastAPI app whose one route runs the real `require_permission`.

    Not `create_app()`: no route in Phase 1 yet takes a resource kind and id from the
    caller, so there is nothing in the real router to drive this through. The app is real,
    the problem handlers are the real ones, and the authorisation call is the production
    function — only the route that reaches it is written here.

    Defined at module scope, and that is not a style choice. `from __future__ import
    annotations` turns every annotation into a string, and FastAPI resolves them with
    `get_type_hints`, which looks in the function's `__globals__` — module globals. A route
    declared inside a method sees `Request` only as a local name, so FastAPI cannot resolve
    it, treats `request` as a query parameter, and answers 422 for a missing `request`
    field. That is what the first run of this file did.
    """
    from src.auth.cerbos import CerbosClient
    from src.core.errors import install_problem_handlers

    app = FastAPI()
    install_problem_handlers(app)
    app.state.cerbos = CerbosClient(cerbos_base_url(), http=httpx.AsyncClient(timeout=10.0))
    app.add_api_route("/probe", _probe, methods=["GET"])
    client = TestClient(app, raise_server_exceptions=False)
    # Entered, and that is load-bearing. An un-entered `TestClient` spins up a fresh anyio
    # portal — and therefore a fresh event loop — per request, while the `httpx.AsyncClient`
    # above is bound to the loop that created its connection pool. The second request then
    # raises `RuntimeError: Event loop is closed` inside the Cerbos call, which the problem
    # handler renders as a 500 and which reads exactly like an authorisation bug.
    client.__enter__()
    return client


async def _probe(request: Request, kind: str, resource_id: str, action: str, owner: str = "") -> dict[str, str]:
    """Ask the real `require_permission` about one (viewer, resource, action) triple."""
    from src.auth.cerbos import CerbosResource
    from src.auth.dependencies import require_permission
    from src.auth.models import UserRole
    from src.auth.principal import Principal

    principal = Principal.for_user(
        user_id=uuid.UUID(PROBE_VIEWER_ID),
        subject="test-only-viewer",
        email="viewer@forgeops.invalid",
        role=UserRole.VIEWER,
    )
    attr = {"owner_id": owner, "member_ids": [owner], "created_by": owner} if owner else {}
    await require_permission(
        request,
        principal,
        resource=CerbosResource(kind=kind, id=resource_id, attr=attr),
        action=action,
    )
    return {"never": "reached"}


class TestTheForbiddenBodyIsNotAnEnumerationOracle:
    """§4.2: a 403 that differs for an unknown id from one the caller may not see lets an
    attacker enumerate ids by reading the difference.

    Compared as **rendered HTTP responses** from a real app running the real
    `require_permission`, not as two calls to the same constructor. Comparing constructor
    output would be comparing a constant with itself; what has to hold is that the bytes on
    the wire are the same when the only thing that differed was the resource.
    """

    @staticmethod
    def _client() -> Any:
        return _probe_client()

    @_TRIPLE_SETTINGS
    @given(
        kind=st.sampled_from(POLICY_DIR_NAMES),
        existing_id=st.uuids().map(str),
        absent_id=st.uuids().map(str),
        action=st.sampled_from([a for a in ACTION_VOCABULARY if a not in NON_MUTATING]),
    )
    def test_two_denials_render_identical_bytes(self, kind: str, existing_id: str, absent_id: str, action: str) -> None:
        import orjson

        client = self._client()

        rendered: list[bytes] = []
        for resource_id, owner_value in ((existing_id, PROBE_VIEWER_ID), (absent_id, "")):
            response = client.get(
                "/probe",
                params={"kind": kind, "resource_id": resource_id, "action": action, "owner": owner_value},
            )
            assert response.status_code == 403, (
                f"viewer was not refused {action!r} on {kind!r}: {response.status_code} {response.text[:200]}"
            )
            payload = response.json()
            payload.pop("instance", None)
            payload.pop("trace_id", None)
            rendered.append(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS))

        assert rendered[0] == rendered[1], (
            "the 403 body differs between a resource the caller is a member of and one that "
            f"does not exist, which is an enumeration oracle: {rendered}"
        )

    def test_the_fixed_detail_names_nothing_caller_specific(self) -> None:
        """What makes the comparison above meaningful rather than two equal constants: the
        constant must not contain anything caller-specific in the first place."""
        from src.core.errors import FORBIDDEN_DETAIL

        for leak in ("secret", "project", "change_set", "read_value", "admin", "viewer", "cerbos"):
            assert leak not in FORBIDDEN_DETAIL.lower(), f"the fixed 403 detail names {leak!r}"

    def test_forbidden_problem_is_the_only_way_to_build_it(self) -> None:
        """A caller that could pass its own `detail` could reintroduce the oracle."""
        import inspect

        from src.core.errors import forbidden_problem

        signature = inspect.signature(forbidden_problem)
        assert "detail" not in signature.parameters, signature
