# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-30 — identity-derived blast radius (design.md §11.2, §13.1, D-39, Appendix B).

Property, universally quantified over principals and environments:

    `blast_radius` is derived from the verified identity; setting
    `MCP_AGENT_BLAST_RADIUS` cannot widen it for an authenticated caller; and the
    variable's presence with `APP_ENV=production` is a startup error.

Why this is a property and not an example
-----------------------------------------
Phase 0 read the blast radius from an environment variable, so the authority of a request
was a property of the *server's configuration* rather than of the caller: two callers with
different authority got the same ceiling, and widening it for one widened it for all (OQ-20,
resolved by D-39). The failure that must be impossible is not "one principal has the wrong
radius" — it is "some route, some code path, some env value can raise a caller's ceiling".
That is a universal claim, so it is quantified: over every role, every attestation kind,
every project grant, and over every value the environment variable can take, including
values wider than the identity permits.

Three clauses, three layers
---------------------------
* **Derivation.** The radius comes out of the identity and there is no way to pass one.
  Asserted structurally — `Principal.for_user` has no `blast_radius` parameter — and
  behaviourally over generated identities.
* **Non-widening.** With every value of `MCP_AGENT_BLAST_RADIUS`, an authenticated caller's
  radius is unchanged. Asserted over a REAL app whose settings carry the variable, because
  the claim is about the composed system and not about a pure function.
* **Production refusal.** `Settings` raises when the variable is present with
  `APP_ENV=production`, for every value including the narrowest.

And the OQ-20 half the leaf names explicitly: `policies/mcp/gateway.rego` is unchanged and
its 27 tests still pass. D-39 changed where the *backend* gets a blast radius; the Rego
policy already keyed on `input.agent_blast_radius` and was deliberately left alone. Asserting
that here is what makes "the policy is untouched" a fact rather than a claim.

Negative control (`mutations.toml` Q-30): make the gateway read the env var when a principal
is present. The property must then fail.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_REGO = REPO_ROOT / "policies" / "mcp" / "gateway.rego"
GATEWAY_REGO_TEST = REPO_ROOT / "policies" / "mcp" / "gateway_test.rego"

RADII = ("read_only", "workspace", "infrastructure")

#: Every value the environment variable can legally take, plus the empty string. `Settings`
#: types it as a `Literal`, so an illegal value is a validation error rather than a widening —
#: which is itself worth asserting, and is why the invalid case appears below.
ENV_VALUES = ("read_only", "workspace", "infrastructure")

ATTESTATIONS = ("paired_device", "spiffe", "unattested", "an-attestation-this-build-never-heard-of")


def _order_index(radius: str) -> int:
    from src.auth.principal import BLAST_RADIUS_ORDER

    return BLAST_RADIUS_ORDER.index(radius)


def _code_without_prose(path: Path) -> str:
    """A module's source with every docstring and comment removed.

    Needed because a structural assertion over raw source reads the explanation as well as
    the behaviour: `principal.py`'s module docstring explains the D-39 history and therefore
    contains the words "environment variable" and `MCP_AGENT_BLAST_RADIUS`. A check that
    matched those was reporting the comment, not the code — and deleting the explanation to
    satisfy the check would have been strictly worse than having no check.

    `ast.unparse` after stripping docstring nodes gives code only; comments never survive
    parsing at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@lru_cache(maxsize=1)
def _committed_rego_digest() -> tuple[str, str]:
    """The digest of the gateway policy and its test file, as committed to git.

    Read from `git show HEAD:<path>` rather than from a constant checked into this file. A
    hard-coded hash would have to be updated by hand every time the policy legitimately
    changed, and the update is indistinguishable from the change it is supposed to police.
    Comparing the working tree against HEAD asserts the thing that actually matters for
    OQ-20: *this* change did not touch the policy.
    """
    digests: list[str] = []
    for path in (GATEWAY_REGO, GATEWAY_REGO_TEST):
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "show", f"HEAD:{path.relative_to(REPO_ROOT).as_posix()}"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, f"could not read {path} from HEAD: {completed.stderr[:200]!r}"
        digests.append(hashlib.sha256(completed.stdout.replace(b"\r\n", b"\n")).hexdigest())
    return digests[0], digests[1]


def _working_tree_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: The minimum environment `Settings()` needs to construct at all. Anything a clause is
#: actually about is layered on top by `environment(...)`.
_BASE_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:5432/d",
    "REDIS_URL": "redis://127.0.0.1:6379/0",
    "MCP_OIDC_AUDIENCE": "forgeops-mcp-gateway",
}


@contextmanager
def environment(**overrides: str | None) -> Iterator[None]:
    """Set environment variables for one Hypothesis example and restore them afterwards.

    `monkeypatch` is deliberately not used here. It is function-scoped, so Hypothesis
    rightly refuses it: the fixture is torn down once while the body runs hundreds of times,
    which means example N+1 would inherit example N's environment. That is not a health
    check worth suppressing — it is the bug the check exists to report, and it is exactly the
    class of leak `test_lifespan_health.py` records having fixed for `DATABASE_URL`.
    """
    previous: dict[str, str | None] = {}
    try:
        for key, value in {**_BASE_ENV, **overrides}.items():
            previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestTheRadiusIsDerivedAndCannotBePassed:
    def test_for_user_has_no_blast_radius_parameter(self) -> None:
        """The structural half. A constructor that accepted one would let a caller widen its
        own authority at a call site that looks entirely reasonable in review."""
        import inspect

        from src.auth.principal import Principal

        for factory in (Principal.for_user, Principal.for_device):
            assert "blast_radius" not in inspect.signature(factory).parameters, factory

    def test_the_principal_is_frozen(self) -> None:
        """Derivation is worthless if a handler can assign over the result."""
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        principal = Principal.for_user(
            user_id=uuid.uuid4(), subject="test-only-subject", email="a@forgeops.invalid", role=UserRole.ADMIN
        )
        with pytest.raises((AttributeError, TypeError)):
            principal.blast_radius = "infrastructure"  # type: ignore[misc]

    @_SETTINGS
    @given(role_name=st.sampled_from(("viewer", "developer", "admin")), subject=st.text(min_size=1, max_size=40))
    def test_a_user_radius_is_a_function_of_the_role_alone(self, role_name: str, subject: str) -> None:
        from src.auth.models import UserRole
        from src.auth.principal import ROLE_BLAST_RADIUS, Principal

        role = UserRole(role_name)
        principal = Principal.for_user(
            user_id=uuid.uuid4(), subject=subject, email=f"{role_name}@forgeops.invalid", role=role
        )
        assert principal.blast_radius == ROLE_BLAST_RADIUS[role]

    @_SETTINGS
    @given(attestation=st.sampled_from(ATTESTATIONS), grant=st.sampled_from(RADII))
    def test_a_device_radius_never_exceeds_either_input(self, attestation: str, grant: str) -> None:
        """The narrowest of the two, never the widest — and an unknown attestation fails
        closed to `read_only` rather than raising, because a device presenting an attestation
        this build does not recognise is exactly the one that must not get write authority."""
        from src.auth.principal import DEVICE_ATTESTATION_BLAST_RADIUS, Principal

        principal = Principal.for_device(
            device_id=uuid.uuid4(), subject="test-only-device", attestation=attestation, project_grant=grant
        )
        from_attestation = DEVICE_ATTESTATION_BLAST_RADIUS.get(attestation, "read_only")
        assert _order_index(principal.blast_radius) <= _order_index(from_attestation)
        assert _order_index(principal.blast_radius) <= _order_index(grant)

    def test_no_attestation_kind_reaches_infrastructure(self) -> None:
        """§14.3 states the gap plainly: Phase 1 has no hardware-rooted device attestation, so
        a device gets infrastructure authority from an approved change-set, never from its own
        identity."""
        from src.auth.principal import DEVICE_ATTESTATION_BLAST_RADIUS

        assert "infrastructure" not in DEVICE_ATTESTATION_BLAST_RADIUS.values(), DEVICE_ATTESTATION_BLAST_RADIUS


class TestTheEnvironmentVariableCannotWidenAnAuthenticatedCaller:
    @_SETTINGS
    @given(
        role_name=st.sampled_from(("viewer", "developer", "admin")),
        env_value=st.sampled_from(ENV_VALUES),
    )
    def test_the_derived_radius_ignores_the_variable(self, role_name: str, env_value: str) -> None:
        """Over a REAL `Settings` carrying the variable, so the claim is about the composed
        system rather than about a pure function that never reads the environment."""
        from src.auth.models import UserRole
        from src.auth.principal import ROLE_BLAST_RADIUS, Principal
        from src.core.config import Settings

        with environment(MCP_AGENT_BLAST_RADIUS=env_value, APP_ENV="development"):
            composed = Settings()
            assert composed.mcp_agent_blast_radius == env_value, "the variable was not actually set"

            role = UserRole(role_name)
            principal = Principal.for_user(
                user_id=uuid.uuid4(), subject="test-only-subject", email="a@forgeops.invalid", role=role
            )
        assert principal.blast_radius == ROLE_BLAST_RADIUS[role], (
            f"MCP_AGENT_BLAST_RADIUS={env_value!r} changed an authenticated caller's radius "
            f"from {ROLE_BLAST_RADIUS[role]!r} to {principal.blast_radius!r}"
        )

    @_SETTINGS
    @given(
        role_name=st.sampled_from(("viewer", "developer")),
        env_value=st.sampled_from(("infrastructure", "workspace")),
    )
    def test_a_wider_variable_never_raises_a_narrower_identity(self, role_name: str, env_value: str) -> None:
        """The sharp case, stated separately because it is the one an attacker wants: the
        variable is set WIDER than the caller's identity permits."""
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        ceiling = {"viewer": "read_only", "developer": "workspace"}[role_name]
        with environment(MCP_AGENT_BLAST_RADIUS=env_value, APP_ENV="development"):
            principal = Principal.for_user(
                user_id=uuid.uuid4(),
                subject="test-only-subject",
                email="a@forgeops.invalid",
                role=UserRole(role_name),
            )
        assert _order_index(principal.blast_radius) <= _order_index(ceiling), (
            f"a {role_name} reached {principal.blast_radius!r} with "
            f"MCP_AGENT_BLAST_RADIUS={env_value!r}; its ceiling is {ceiling!r}"
        )

    def test_the_gateway_default_applies_only_when_there_is_no_principal(self) -> None:
        """D-39 keeps the variable as a DEV default for the principal-less case. That is the
        one place it may be read, and `require_mcp_principal` documents why: a machine token
        that carries no role resolves to `viewer`, and every mutating path needs a minted
        authority regardless."""
        import inspect

        from src.auth import dependencies

        source = inspect.getsource(dependencies)
        assert "mcp_agent_blast_radius" not in source, (
            "the auth dependencies read MCP_AGENT_BLAST_RADIUS; a principal's radius must come from its identity (D-39)"
        )

    def test_the_principal_module_never_reads_the_environment(self) -> None:
        """The strongest structural statement available: the module that derives the radius
        cannot read any variable, because it never reaches the environment.

        Asserted over the module's CODE with docstrings and comments removed. The first draft
        matched the word "environment" in the module docstring — which explains the D-39
        history and is exactly the prose a reader needs — so the check was reporting the
        explanation rather than the behaviour.
        """
        from src.auth import principal as principal_module

        code = _code_without_prose(Path(principal_module.__file__))
        for forbidden in ("os.environ", "getenv", "environ[", "MCP_AGENT_BLAST_RADIUS"):
            assert forbidden not in code, f"{forbidden!r} appears in the derivation module's code: {code[:300]}"


class TestProductionRefusesTheVariableOutright:
    @_SETTINGS
    @given(env_value=st.sampled_from(ENV_VALUES))
    def test_settings_raises_for_every_value(self, env_value: str) -> None:
        """Including `read_only`, the narrowest. The rule is about the variable being present
        at all, not about the value being dangerous — a variable that is honoured in
        production is one somebody will widen in production."""
        from pydantic import ValidationError
        from src.core.config import Settings

        with environment(APP_ENV="production", MCP_AGENT_BLAST_RADIUS=env_value):
            with pytest.raises(ValidationError) as caught:
                Settings()
        assert "MCP_AGENT_BLAST_RADIUS" in str(caught.value)

    def test_the_same_environment_without_the_variable_gets_past_this_check(self) -> None:
        """The vacuity guard. A production environment that failed for some *other* missing
        credential would make the clause above pass whatever it did with the variable, so the
        failure must be attributable to the variable and to nothing else."""
        from pydantic import ValidationError
        from src.core.config import Settings

        with environment(APP_ENV="production", MCP_AGENT_BLAST_RADIUS=None):
            try:
                Settings()
            except ValidationError as exc:
                assert "MCP_AGENT_BLAST_RADIUS" not in str(exc), (
                    "production still complains about the variable when it is absent, so the "
                    "clause above proves nothing about the variable"
                )


class TestTheGatewayPolicyIsUntouchedAsOq20Anticipated:
    """D-39 changed where the BACKEND gets a blast radius. The Rego policy already keyed on
    `input.agent_blast_radius`, so it was deliberately left alone — and this is what turns
    "the policy is untouched" from a claim into a fact."""

    def test_the_policy_files_match_head_byte_for_byte(self) -> None:
        committed_policy, committed_test = _committed_rego_digest()
        assert _working_tree_digest(GATEWAY_REGO) == committed_policy, (
            "policies/mcp/gateway.rego differs from HEAD. OQ-20's whole point is that the "
            "Rego half needed no change; if it genuinely needs one, that is a decision to "
            "record, not a diff to slip in beside a property test."
        )
        assert _working_tree_digest(GATEWAY_REGO_TEST) == committed_test

    def test_the_policy_still_keys_on_the_input_field_not_an_environment_variable(self) -> None:
        source = GATEWAY_REGO.read_text(encoding="utf-8")
        assert "input.agent_blast_radius" in source, source[:200]
        assert "MCP_AGENT_BLAST_RADIUS" not in source

    def test_all_twenty_seven_rego_tests_still_pass(self) -> None:
        """Run with the real OPA, not asserted from a count in a document.

        Through the **digest-pinned image** by preference rather than a local `opa` binary,
        for two reasons. The version is then the one `docker-compose.yml` pins — read out of
        it, so it cannot drift from what the compose checks police — rather than whatever
        happens to be on PATH; and the `backend` job installs no `opa` binary (it starts the
        container from a test), so requiring one would have made this clause fail in CI for a
        missing tool. A local binary is still accepted as a fallback for a developer without
        a Docker daemon.
        """
        from tests.integration.capability import require_capability

        argv = self._opa_test_argv()
        if argv is None:
            require_capability(
                "opa",
                "neither a Docker daemon (for the digest-pinned OPA image) nor an `opa` "
                "binary on PATH is available to run the gateway policy tests",
            )

        completed = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]

        # Two independent readings of the same run, because each alone can lie. The
        # per-test lines are `data.mcp.gateway_test.test_x: PASS (0s)`; the summary is
        # `PASS: 27/27`. Counting only "PASS:" matches the summary line and nothing else,
        # which is how the first draft of this assertion reported 1.
        per_test = completed.stdout.count(": PASS (")
        summary = re.search(r"^PASS:\s*(\d+)/(\d+)\s*$", completed.stdout, re.MULTILINE)
        assert summary is not None, completed.stdout[-1000:]
        assert summary.group(1) == summary.group(2), f"not every test passed: {summary.group(0)}"
        assert per_test == 27 == int(summary.group(1)), (
            f"expected 27 passing gateway policy tests; `opa test` reported {per_test} "
            f"per-test PASS lines and a summary of {summary.group(0)!r}. OQ-20 anticipated "
            "the count staying put; a change here is a policy change."
        )

    @staticmethod
    def _opa_test_argv() -> list[str] | None:
        """The command that runs the gateway policy tests, or `None` if neither route exists."""
        docker = shutil.which("docker")
        if docker is not None:
            compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
            image = re.search(r"openpolicyagent/opa:[^\s\"']+@sha256:[0-9a-f]{64}", compose)
            assert image is not None, "no digest-pinned openpolicyagent/opa reference in docker-compose.yml"
            probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                return [
                    docker,
                    "run",
                    "--rm",
                    "-v",
                    f"{GATEWAY_REGO.parent}:/policies:ro",
                    image.group(0),
                    "test",
                    "/policies",
                    "-v",
                ]

        binary = shutil.which("opa") or os.environ.get("FORGEOPS_OPA_BIN", "")
        if binary:
            return [binary, "test", str(GATEWAY_REGO.parent), "-v"]
        return None


class TestTheDerivationTableIsNotEmpty:
    """Every clause above enumerates a mapping. An emptied mapping would make them vacuous —
    the exact shape of the P-09 defect, where both redaction lists were emptied and thirteen
    tests stayed green."""

    def test_the_role_mapping_covers_every_role(self) -> None:
        from src.auth.models import UserRole
        from src.auth.principal import ROLE_BLAST_RADIUS

        assert set(ROLE_BLAST_RADIUS) == set(UserRole), ROLE_BLAST_RADIUS
        assert set(ROLE_BLAST_RADIUS.values()) == set(RADII), ROLE_BLAST_RADIUS

    def test_the_attestation_mapping_is_populated(self) -> None:
        from src.auth.principal import DEVICE_ATTESTATION_BLAST_RADIUS

        assert len(DEVICE_ATTESTATION_BLAST_RADIUS) >= 3, DEVICE_ATTESTATION_BLAST_RADIUS

    def test_the_order_is_a_total_order_over_the_three_radii(self) -> None:
        from src.auth.principal import BLAST_RADIUS_ORDER

        assert tuple(BLAST_RADIUS_ORDER) == RADII, BLAST_RADIUS_ORDER
