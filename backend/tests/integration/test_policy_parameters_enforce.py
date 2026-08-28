# SPDX-License-Identifier: FSL-1.1-ALv2
"""Stored policies must actually constrain something (FR-32, FR-33).

`chokepoint.py` sent `"policy_parameters": {}` on every stage-1 evaluation, with a comment stating the
consequence in as many words: "no blocked weekday blocks nothing, no glob protects nothing". The
bundle's `schedule.rego` and `paths.rego` were correct all along and were reading an empty object, so a
user could write a policy, see it listed as enabled, and have it restrict nothing — while the
completion criterion "Policies are enforced (block Friday deploys, require approvals)" was ticked and
the UI offered full policy CRUD.

This file covers the link that was missing: a row in `policies` becomes `input.project`. The other two
links have tests already and are cited rather than duplicated — `test_policies_crud.py` covers the CRUD
surface, and `test_governance_policy_opa.py` proves the real bundle denies on `schedule.blocked_window`
and `paths.protected_path` when those parameters are present. Chain: stored row → merged parameters →
bundle refusal.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.governance.chokepoint import load_policy_parameters, merge_policy_parameters
from src.policies.opa import PROJECT_PARAMETER_KEYS, governance_input

# ── the merge, as a pure function ────────────────────────────────────────────────────────────────


def test_list_valued_parameters_union_rather_than_overwrite() -> None:
    """Adding a policy must not be able to REMOVE a restriction."""
    merged = merge_policy_parameters([{"blocked_weekdays": ["Friday"]}, {"blocked_weekdays": ["Saturday"]}])
    assert merged["blocked_weekdays"] == ["Friday", "Saturday"], (
        "two policies each blocking a day must block both; last-write-wins would mean adding a "
        "policy silently removed Friday"
    )


def test_the_union_is_deterministically_ordered() -> None:
    """Two evaluations over unchanged rows must produce an identical document."""
    forward = merge_policy_parameters([{"protected_globs": ["b", "a"]}, {"protected_globs": ["c"]}])
    backward = merge_policy_parameters([{"protected_globs": ["c"]}, {"protected_globs": ["a", "b"]}])
    assert forward["protected_globs"] == backward["protected_globs"] == ["a", "b", "c"]


def test_duplicate_entries_across_policies_appear_once() -> None:
    merged = merge_policy_parameters([{"protected_globs": ["**/package.json"]}] * 3)
    assert merged["protected_globs"] == ["**/package.json"]


def test_a_scalar_takes_the_first_non_empty_value() -> None:
    """A contradiction is resolved deterministically, not by inventing a third answer."""
    merged = merge_policy_parameters([{"timezone": ""}, {"timezone": "Asia/Kolkata"}, {"timezone": "Europe/London"}])
    assert merged["timezone"] == "Asia/Kolkata"


def test_blocked_window_merges_key_wise() -> None:
    """A policy setting only start_hour must not erase another's end_hour."""
    merged = merge_policy_parameters([{"blocked_window": {"start_hour": 6}}, {"blocked_window": {"end_hour": 20}}])
    assert merged["blocked_window"] == {"start_hour": 6, "end_hour": 20}


def test_a_list_beats_a_scalar_under_the_same_key() -> None:
    """When two policies contradict each other in kind, restrictions must survive."""
    merged = merge_policy_parameters([{"blocked_weekdays": "Friday"}, {"blocked_weekdays": ["Monday"]}])
    assert merged["blocked_weekdays"] == ["Monday"]


def test_no_rows_is_an_empty_document_rather_than_an_error() -> None:
    """A project with no policies must produce a DEFINED allow, not an undefined document."""
    assert merge_policy_parameters([]) == {}


def test_a_non_mapping_row_is_ignored_rather_than_crashing_a_mutation() -> None:
    """`parameters` is JSONB, so a hand-edited row could hold a list. Stage 1 must survive it."""
    rows: list[Any] = [["not", "a", "mapping"], {"timezone": "UTC"}]
    assert merge_policy_parameters(rows) == {"timezone": "UTC"}


def test_the_merge_does_not_duplicate_the_authoritative_key_list() -> None:
    """§2.2.1 forbids `governance/` importing `src.policies`, so the filter stays on one side.

    The merge deliberately passes unknown keys through, because `policies.opa._project_parameters`
    applies `PROJECT_PARAMETER_KEYS` when the payload becomes an input document. Two copies of that
    list is the drift the cross-domain ban exists to prevent, so this asserts the division of labour:
    the merge keeps everything, the projection drops what the bundle cannot read.
    """
    merged = merge_policy_parameters([{"blocked_weekdays": ["Friday"], "invented_key": ["x"]}])
    assert "invented_key" in merged, "the merge is not the place the closed list is applied"
    document = governance_input({"policy_parameters": merged, "operation": "changeset.apply"})
    assert "invented_key" not in document["project"], "the projection failed to drop an unknown key"
    assert document["project"]["blocked_weekdays"] == ["Friday"]


def test_every_declared_parameter_key_survives_the_round_trip() -> None:
    """A key the bundle reads must not be dropped between the row and `input.project`."""
    row: dict[str, Any] = {
        "timezone": "Asia/Kolkata",
        "blocked_weekdays": ["Friday"],
        "blocked_operations": ["changeset.apply"],
        "blocked_window": {"start_hour": 6, "end_hour": 20},
        "protected_globs": ["**/package.json"],
    }
    assert set(row) == set(PROJECT_PARAMETER_KEYS), (
        "this fixture must cover every key the bundle reads, or the round trip it checks is partial"
    )
    document = governance_input({"policy_parameters": merge_policy_parameters([row]), "operation": "changeset.apply"})
    assert document["project"] == row


# ── the load, against a real database ───────────────────────────────────────────────────────────


async def _new_project(session: AsyncSession) -> uuid.UUID:
    project_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": project_id, "name": f"policy-params-{project_id.hex[:8]}", "path": "/tmp/policy"},
    )
    return project_id


async def _insert_policy(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    name: str,
    parameters: dict[str, Any],
    enabled: bool = True,
) -> None:
    """Insert with raw SQL, the way the loader reads it, so the column really is exercised."""
    await session.execute(
        text(
            "INSERT INTO policies (id, project_id, tenant_id, name, engine, rego_rules, enabled, "
            "parameters) VALUES (:id, :project, NULL, :name, 'rego', 'package forgeops.unused', "
            ":enabled, CAST(:parameters AS jsonb))"
        ),
        {
            "id": uuid.uuid4(),
            "project": project_id,
            "name": name,
            "enabled": enabled,
            "parameters": json.dumps(parameters),
        },
    )


@pytest.mark.asyncio
async def test_an_enabled_policys_parameters_reach_the_loader(sessions: Any) -> None:
    """The link that was missing: a row in `policies` becomes `input.project`."""
    async with sessions() as session:
        project_id = await _new_project(session)
        await _insert_policy(
            session,
            project_id=project_id,
            name="no friday deploys",
            parameters={"blocked_weekdays": ["Friday"], "timezone": "Asia/Kolkata"},
        )
        await session.flush()

        parameters = await load_policy_parameters(session, project_id)
        assert parameters["blocked_weekdays"] == ["Friday"]
        assert parameters["timezone"] == "Asia/Kolkata"

        # And it survives the projection into the document the bundle is actually asked about, which
        # is what makes this end to end rather than a test of one SELECT.
        document = governance_input({"policy_parameters": parameters, "operation": "changeset.apply"})
        assert document["project"]["blocked_weekdays"] == ["Friday"]
        await session.rollback()


@pytest.mark.asyncio
async def test_a_disabled_policy_contributes_nothing(sessions: Any) -> None:
    """Disabling a policy must stop the NEXT mutation, not one after a cache expires."""
    async with sessions() as session:
        project_id = await _new_project(session)
        await _insert_policy(
            session,
            project_id=project_id,
            name="switched off",
            parameters={"protected_globs": ["**/package.json"]},
            enabled=False,
        )
        await session.flush()

        parameters = await load_policy_parameters(session, project_id)
        assert "protected_globs" not in parameters, "a disabled policy still restricted something"
        await session.rollback()


@pytest.mark.asyncio
async def test_a_global_policy_applies_to_every_project(sessions: Any) -> None:
    """`policies.project_id IS NULL` means global; omitting those would make it silently local."""
    async with sessions() as session:
        project_id = await _new_project(session)
        await _insert_policy(
            session,
            project_id=None,
            name=f"global protected paths {uuid.uuid4().hex[:8]}",
            parameters={"protected_globs": ["**/package.json"]},
        )
        await session.flush()

        parameters = await load_policy_parameters(session, project_id)
        assert parameters["protected_globs"] == ["**/package.json"]
        await session.rollback()


@pytest.mark.asyncio
async def test_another_projects_policy_does_not_leak(sessions: Any) -> None:
    """Scoping, in the direction that matters: one project's restriction is not another's."""
    async with sessions() as session:
        mine = await _new_project(session)
        theirs = await _new_project(session)
        await _insert_policy(
            session,
            project_id=theirs,
            name="their fridays",
            parameters={"blocked_weekdays": ["Friday"]},
        )
        await session.flush()

        assert await load_policy_parameters(session, mine) == {}
        await session.rollback()


@pytest.mark.asyncio
async def test_two_policies_on_one_project_both_apply(sessions: Any) -> None:
    async with sessions() as session:
        project_id = await _new_project(session)
        await _insert_policy(
            session,
            project_id=project_id,
            name="fridays",
            parameters={"blocked_weekdays": ["Friday"]},
        )
        await _insert_policy(
            session,
            project_id=project_id,
            name="manifests",
            parameters={"protected_globs": ["**/package.json"]},
        )
        await session.flush()

        parameters = await load_policy_parameters(session, project_id)
        assert parameters["blocked_weekdays"] == ["Friday"]
        assert parameters["protected_globs"] == ["**/package.json"]
        await session.rollback()


@pytest.mark.asyncio
async def test_a_project_with_no_policies_gets_a_defined_empty_document(sessions: Any) -> None:
    """The state every project starts in, and the reason the bundle's totality matters."""
    async with sessions() as session:
        project_id = await _new_project(session)
        assert await load_policy_parameters(session, project_id) == {}
        await session.rollback()
