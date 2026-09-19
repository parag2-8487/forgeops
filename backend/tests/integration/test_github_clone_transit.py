# SPDX-License-Identifier: FSL-1.1-ALv2
"""The clone transit through the REAL chokepoint (design §2.2, §11.6).

WHAT THESE PIN, and why each is here rather than assumed:

* a clone passes all six stages. Asserted by the artifacts each stage leaves — a `change_sets` row, a
  blast-radius verdict, an audit event, a rollback handle and a signed envelope — rather than by reading
  the method, because "it calls the right functions" is what a refactor breaks silently.
* the CREDENTIAL IS IN THE ENVELOPE AND NOWHERE ELSE. Asserted over the whole audit row, the whole
  change-set row and the whole serialised blast-radius state, not field by field, so a column added
  later cannot carry it past this test.
* a policy that requires approval STOPS the clone. A clone that auto-approved past a
  `require_approval` verdict would put someone else's code on an operator's machine without the human
  the policy asked for.
* a policy denial refuses it, and the refusal is audited.
* the envelope names `repository.clone` and carries the arguments the agent validates — the operation
  string is the one thing both sides must agree on, and `test_agent_backend_policy_agreement.py` exists
  because they once did not.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.chokepoint_support import (
    RecordingSink,
    ScriptedPolicy,
    allow,
    build_chokepoint,
    deny,
    make_fixture,
    require_approval,
)

# `sessions`, `redis_client` and `sink` are NOT imported. They are fixtures, `conftest.py` re-exports
# them for discovery, and importing them by NAME shadows this module's own parameters of the same name —
# F811 at every signature. `chokepoint_support.py`'s own note records why they live there.

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

#: The credential the transit is handed. Self-labelling and synthetic: what is under test is where it
#: travels, not what it looks like.
CLONE_CREDENTIAL = "github-clone-credential-for-this-test-only"

REPO = "octo-org/deploy-me"
CLONE_URL = "https://github.com/octo-org/deploy-me.git"


async def _clone(chokepoint: Any, session: AsyncSession, fixture: Any) -> Any:
    return await chokepoint.clone_repository(
        session,
        project_id=fixture.project_id,
        principal=fixture.principal,
        repo_full_name=REPO,
        clone_url=CLONE_URL,
        parent_directory="/srv/workspaces",
        directory_name="deploy-me",
        branch="main",
        credential=CLONE_CREDENTIAL,
        reason="requested by the clone transit test",
    )


async def _rows(session: AsyncSession, query: str, **params: Any) -> list[dict[str, Any]]:
    result = await session.execute(text(query), params)
    return [dict(row) for row in result.mappings()]


class TestTheCloneTransit:
    async def test_it_passes_every_stage_and_delivers_a_signed_envelope(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)

            submission = await _clone(chokepoint, session, fixture)

            assert submission.outcome == "applying"
            assert submission.status == "applying"
            assert submission.command is not None

            # Stage 3: a change set exists, with zero items — a clone writes no files, and a synthetic
            # item would be a row claiming a write nobody made.
            sets = await _rows(
                session,
                "SELECT status, origin, blast_radius_verdict, version FROM change_sets WHERE id = :id",
                id=submission.change_set_id,
            )
            assert sets and sets[0]["status"] == "applying"
            items = await _rows(
                session,
                "SELECT count(*) AS n FROM change_items WHERE change_set_id = :id",
                id=submission.change_set_id,
            )
            assert items[0]["n"] == 0

            # Stage 4: the analyser's verdict is recorded rather than asserted by the transit.
            assert sets[0]["blast_radius_verdict"] in {"allow", "warn"}

            # Stage 5: exactly one audit row for the transit, naming the repository.
            audit = await _rows(
                session,
                "SELECT action, outcome, reason, after_state::text AS after FROM audit_events "
                "WHERE resource_id = :id ORDER BY seq",
                id=str(submission.change_set_id),
            )
            assert len(audit) == 1, audit
            assert audit[0]["outcome"] == "allowed"
            assert REPO in audit[0]["reason"]

            # Stage 6: the rollback handle exists before the envelope was delivered.
            handles = await _rows(
                session,
                "SELECT count(*) AS n FROM rollback_handles WHERE change_set_id = :id",
                id=submission.change_set_id,
            )
            assert handles[0]["n"] == 1

        # The mint: one command, to this fixture's device, naming the operation both sides agree on.
        assert len(sink.sent) == 1
        device_id, command = sink.sent[0]
        assert device_id == fixture.device_id
        assert command.envelope["operation"] == "repository.clone"
        args = command.envelope["args"]
        assert args["repo_full_name"] == REPO
        assert args["clone_url"] == CLONE_URL
        assert args["directory_name"] == "deploy-me"
        assert args["parent_directory"] == "/srv/workspaces"
        assert args["branch"] == "main"
        # The credential IS here — this is the only place it may be.
        assert args["token"] == CLONE_CREDENTIAL

    async def test_the_credential_reaches_no_row_at_all(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Asserted over whole rows rather than named columns, so a later column cannot carry it."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            submission = await _clone(chokepoint, session, fixture)

            change_sets = await _rows(
                session,
                "SELECT to_jsonb(change_sets) AS row FROM change_sets WHERE id = :id",
                id=submission.change_set_id,
            )
            audit = await _rows(
                session,
                "SELECT to_jsonb(audit_events) AS row FROM audit_events WHERE project_id = :p",
                p=fixture.project_id,
            )
            handles = await _rows(
                session,
                "SELECT to_jsonb(rollback_handles) AS row FROM rollback_handles WHERE change_set_id = :id",
                id=submission.change_set_id,
            )

        serialised = json.dumps([r["row"] for r in change_sets + audit + handles], default=str)
        assert CLONE_CREDENTIAL not in serialised, "the credential reached a database row"
        # And the audit row names the repository, so the absence above is not simply an empty record.
        assert REPO in serialised

    async def test_a_policy_that_requires_approval_stops_the_clone(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)

            submission = await _clone(chokepoint, session, fixture)

            assert submission.outcome == "approval-required"
            assert submission.status == "pending_approval"
            assert submission.command is None
            rows = await _rows(
                session,
                "SELECT action, outcome FROM audit_events WHERE resource_id = :id",
                id=str(submission.change_set_id),
            )
            assert rows and rows[0]["outcome"] == "pending"
        # NOTHING WAS SENT. A clone that reached an agent while a human was still being asked would be
        # the policy working and the transit ignoring it.
        assert sink.sent == []

    async def test_a_denial_refuses_and_is_audited(self, sessions: Any, redis_client: Any, sink: RecordingSink) -> None:
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=deny()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)

            with pytest.raises(Exception) as caught:  # noqa: PT011 - the registered problem type
                await _clone(chokepoint, session, fixture)

            assert "polic" in str(caught.value).lower()
            rows = await _rows(
                session,
                "SELECT outcome FROM audit_events WHERE project_id = :p ORDER BY seq DESC",
                p=fixture.project_id,
            )
            assert rows, "a denial must be audited"
            assert rows[0]["outcome"] in {"denied", "blocked"}
        assert sink.sent == []

    async def test_a_revoked_device_cannot_be_cloned_for(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Stage 0. Admission is the same object the apply path uses, and this proves it runs here."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session, device_status="revoked")

            with pytest.raises(Exception):  # noqa: B017, PT011 - a registered problem type
                await _clone(chokepoint, session, fixture)

        assert sink.sent == []

    async def test_the_envelope_carries_one_sequence_number_per_command(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """Two clones for one device must not reuse a sequence number; the agent's replay guard is real."""
        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            await _clone(chokepoint, session, fixture)
            await _clone(chokepoint, session, fixture)

        assert len(sink.sent) == 2
        first, second = (command.envelope["seq"] for _, command in sink.sent)
        assert second > first
        nonces = {command.envelope["nonce"] for _, command in sink.sent}
        assert len(nonces) == 2, "two commands shared a nonce"


class TestTheApprovalPathRefusesWhatItCannotBuild:
    """The defect a real clone against a real agent exposed, pinned so it cannot return.

    `approve()` ends in `_deliver(operation=changeset.apply, args=_apply_entries(...))`. A clone reached
    the agent through it as an apply with no entries; the agent refused correctly and the change set
    read `rolled_back`, which looks like an agent fault and was the backend sending the wrong command.
    Every test passed at the time, because the AUTO-APPROVED clone path mints its own envelope and never
    goes through `approve()` — so the test that would have caught it is this one.
    """

    async def test_a_pending_clone_cannot_be_approved_into_an_apply(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            submission = await _clone(chokepoint, session, fixture)
            assert submission.status == "pending_approval"

            with pytest.raises(Exception) as caught:  # noqa: PT011 - the registered problem type
                await chokepoint.approve(session, change_set_id=submission.change_set_id, principal=fixture.principal)

            # The refusal NAMES the operation and why, read from the problem DOCUMENT rather than the
            # exception's string form — `str()` on a `ProblemException` is the registry title, which is
            # the same sentence for every conflict and would make this assertion pass against any of them.
            detail = str(getattr(caught.value.problem, "detail", "") or "")
            assert "repository.clone" in detail, detail
            assert "not stored" in detail, detail

        # AND NOTHING WAS SENT. The whole point: a malformed command must not reach the agent.
        assert sink.sent == []

    async def test_an_apply_still_approves_normally(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """The guard must not have closed the door it was standing beside."""
        from src.governance.chokepoint import MutationRequest

        from tests.integration.chokepoint_support import one_create

        chokepoint = build_chokepoint(
            policy=ScriptedPolicy(decision=require_approval()), sink=sink, redis_client=redis_client
        )
        async with sessions() as session:
            fixture = await make_fixture(session)
            submitted = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=fixture.project_id,
                    items=one_create(),
                    reason="the guard must not block an apply",
                ),
                principal=fixture.principal,
            )
            assert submitted.status == "pending_approval"

            approved = await chokepoint.approve(
                session, change_set_id=submitted.change_set_id, principal=fixture.principal
            )

        assert approved.status == "applying"
        assert len(sink.sent) == 1
        assert sink.sent[0][1].envelope["operation"] == "changeset.apply"


class TestTheOperationIsOnTheWhitelist:
    async def test_the_mint_refuses_an_operation_outside_the_catalogue(
        self, sessions: Any, redis_client: Any, sink: RecordingSink
    ) -> None:
        """The whitelist is the point: widening it for the clone must not have opened it generally."""
        from src.governance.chokepoint import APPLY_OPERATION, CLONE_OPERATION, REVERT_OPERATION

        chokepoint = build_chokepoint(policy=ScriptedPolicy(decision=allow()), sink=sink, redis_client=redis_client)
        async with sessions() as session:
            fixture = await make_fixture(session)
            with pytest.raises(ValueError, match="not a mutating operation"):
                await chokepoint._mint_and_sign(  # noqa: SLF001 - the guard under test is private
                    session,
                    change_set_id=uuid.uuid4(),
                    admitted=await chokepoint._admit(  # noqa: SLF001
                        session, project_id=fixture.project_id, principal=fixture.principal
                    ),
                    approval_id=uuid.uuid4(),
                    audit_seq=1,
                    decision=allow(),
                    report=None,
                    operation="shell.exec",
                    args={},
                )
        assert {APPLY_OPERATION, REVERT_OPERATION, CLONE_OPERATION} == {
            "changeset.apply",
            "changeset.revert",
            "repository.clone",
        }
