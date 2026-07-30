# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0005_policies_and_bundles` against a REAL PostgreSQL.

design.md §6.1, §6.3, §6.5, §11.7; tasks.md leaf 5.4.

The headline assertion is that **two active global bundles are impossible**. It is
worth stating why that needs its own index. SQL treats NULLs as distinct in a unique
index, so `UNIQUE (project_id) WHERE active` — the per-project rule — places no
constraint at all on rows where `project_id IS NULL`: two active global bundles do
not collide under it. A single index would therefore have looked correct and enforced
half of what it claimed. The second index constrains `active` within the filtered set
where it is always `true`, which admits exactly one row.

The permissive cases are asserted too. An index that forbade many *inactive* bundles
would destroy the provenance of every device that pinned a superseded digest, and one
that forbade an active global bundle alongside an active project bundle would make
project-scoped policy impossible. Both are the over-constraining failure that a
"two actives are rejected" test on its own would not notice.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from src.policies.models import EVALUATION_RESULTS, EVALUATION_SIDES

from .migration_support import make_project, scalar

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

INSERT_BUNDLE = text(
    "INSERT INTO policy_bundles (id, digest, bundle, project_id, active) "
    "VALUES (:id, :digest, :bundle, :project_id, :active)"
)

INSERT_POLICY = text(
    "INSERT INTO policies (id, project_id, name, engine, rego_rules, enabled) "
    "VALUES (:id, :project_id, :name, 'rego', :rego, true)"
)


def _digest() -> str:
    return "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32]


async def _bundle(conn, *, project_id: uuid.UUID | None, active: bool, digest: str | None = None) -> uuid.UUID:
    bundle_id = uuid.uuid4()
    await conn.execute(
        INSERT_BUNDLE,
        {
            "id": bundle_id,
            "digest": digest or _digest(),
            "bundle": b"not-a-real-bundle",
            "project_id": project_id,
            "active": active,
        },
    )
    return bundle_id


class TestBundleDigestUniqueness:
    async def test_a_duplicate_digest_is_rejected(self, conn) -> None:
        """The digest identifies the artifact. Two rows with one digest would mean two
        different byte streams claiming to be the same bundle."""
        shared = _digest()
        await _bundle(conn, project_id=None, active=False, digest=shared)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _bundle(conn, project_id=None, active=False, digest=shared)


class TestExactlyOneActiveBundlePerScope:
    async def test_two_active_global_bundles_are_rejected(self, conn) -> None:
        """The leaf's headline assertion."""
        await _bundle(conn, project_id=None, active=True)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _bundle(conn, project_id=None, active=True)

    async def test_two_active_bundles_for_one_project_are_rejected(self, conn) -> None:
        project_id = await make_project(conn, "bundles")
        await _bundle(conn, project_id=project_id, active=True)
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _bundle(conn, project_id=project_id, active=True)

    async def test_many_inactive_bundles_are_allowed_globally(self, conn) -> None:
        """Superseded bundles are kept: a device that pinned an old digest must still
        be explainable."""
        for _ in range(4):
            await _bundle(conn, project_id=None, active=False)
        count = await scalar(conn, "SELECT count(*) FROM policy_bundles WHERE project_id IS NULL AND NOT active")
        assert count >= 4

    async def test_many_inactive_bundles_are_allowed_per_project(self, conn) -> None:
        project_id = await make_project(conn, "bundles-inactive")
        for _ in range(4):
            await _bundle(conn, project_id=project_id, active=False)
        count = await scalar(
            conn,
            "SELECT count(*) FROM policy_bundles WHERE project_id = :p AND NOT active",
            p=project_id,
        )
        assert count == 4

    async def test_an_active_global_and_an_active_project_bundle_coexist(self, conn) -> None:
        """The over-constraining case. If this failed, project-scoped policy could
        not exist alongside a global default."""
        project_id = await make_project(conn, "bundles-both")
        await _bundle(conn, project_id=None, active=True)
        await _bundle(conn, project_id=project_id, active=True)

    async def test_two_projects_may_each_have_an_active_bundle(self, conn) -> None:
        first = await make_project(conn, "bundles-p1")
        second = await make_project(conn, "bundles-p2")
        await _bundle(conn, project_id=first, active=True)
        await _bundle(conn, project_id=second, active=True)

    @pytest.mark.parametrize(
        "index",
        ["uq_policy_bundles_one_active_per_project", "uq_policy_bundles_one_active_global"],
    )
    async def test_the_index_is_partial_and_unique(self, conn, index: str) -> None:
        """Asserted on the server's own rendering, so a non-partial or non-unique
        index cannot masquerade as the intended one."""
        definition = await scalar(
            conn,
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n",
            n=index,
        )
        assert definition is not None, f"{index} does not exist"
        assert "UNIQUE" in str(definition), definition
        assert "WHERE" in str(definition), definition


class TestPolicyEvaluationSideAndResult:
    @pytest.mark.parametrize("side", EVALUATION_SIDES)
    async def test_both_sides_are_accepted(self, conn, side: str) -> None:
        """`side` is what makes double evaluation auditable: a disagreement between
        the backend and the agent becomes a row you can query for rather than an
        invisible bug (§1.10)."""
        await conn.execute(
            text(
                "INSERT INTO policy_evaluations (id, operation, result, reason, side) "
                "VALUES (:id, 'fileops.apply', 'allow', 'proof', :side)"
            ),
            {"id": uuid.uuid4(), "side": side},
        )

    async def test_a_third_side_is_rejected(self, conn) -> None:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO policy_evaluations (id, operation, result, reason, side) "
                        "VALUES (:id, 'fileops.apply', 'allow', 'proof', 'frontend')"
                    ),
                    {"id": uuid.uuid4()},
                )

    @pytest.mark.parametrize("result", EVALUATION_RESULTS)
    async def test_every_declared_result_is_accepted(self, conn, result: str) -> None:
        await conn.execute(
            text(
                "INSERT INTO policy_evaluations (id, operation, result, reason, side) "
                "VALUES (:id, 'fileops.apply', :result, 'proof', 'backend')"
            ),
            {"id": uuid.uuid4(), "result": result},
        )

    async def test_an_unknown_result_is_rejected(self, conn) -> None:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO policy_evaluations (id, operation, result, reason, side) "
                        "VALUES (:id, 'fileops.apply', 'probably', 'proof', 'backend')"
                    ),
                    {"id": uuid.uuid4()},
                )


class TestPolicyScoping:
    async def test_a_global_policy_has_a_null_project(self, conn) -> None:
        policy_id = uuid.uuid4()
        await conn.execute(
            INSERT_POLICY,
            {
                "id": policy_id,
                "project_id": None,
                "name": f"global-{policy_id.hex[:8]}",
                "rego": "package forgeops\ndefault allow := false\n",
            },
        )
        scope = await scalar(conn, "SELECT project_id FROM policies WHERE id = :id", id=policy_id)
        assert scope is None

    async def test_a_duplicate_name_within_one_project_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "policy-names")
        name = f"no-friday-deploys-{uuid.uuid4().hex[:8]}"
        await conn.execute(
            INSERT_POLICY,
            {"id": uuid.uuid4(), "project_id": project_id, "name": name, "rego": "package p"},
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    INSERT_POLICY,
                    {
                        "id": uuid.uuid4(),
                        "project_id": project_id,
                        "name": name,
                        "rego": "package p",
                    },
                )

    async def test_the_same_name_in_two_projects_is_fine(self, conn) -> None:
        first = await make_project(conn, "policy-p1")
        second = await make_project(conn, "policy-p2")
        name = f"no-friday-deploys-{uuid.uuid4().hex[:8]}"
        for project_id in (first, second):
            await conn.execute(
                INSERT_POLICY,
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "name": name,
                    "rego": "package p",
                },
            )

    async def test_an_unknown_engine_is_rejected(self, conn) -> None:
        """Phase 1 has exactly one policy engine. A second would need its own
        evaluation path on both sides of the double evaluation."""
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO policies (id, name, engine, rego_rules, enabled) "
                        "VALUES (:id, :name, 'cedar', 'x', true)"
                    ),
                    {"id": uuid.uuid4(), "name": f"cedar-{uuid.uuid4().hex[:8]}"},
                )
