"""Submit a change set through the real governance chokepoint, then approve it.

WHY THIS IS NOT A SHORTCUT PAST THE ARCHITECTURE. `GovernanceChokepoint.submit` is the single
sanctioned mutation path (§2.2's diagram, D-08): it admits the project, evaluates the real policy
bundle, computes blast radius, writes the audit record and mints the signed envelope. Every one of
those runs here. What is skipped is the LLM that would have PROPOSED the file contents — generation
is a source of change items, not a link in the apply chain — and the OIDC round trip that would have
produced the `Principal`.

That distinction matters because this is the only way to exercise apply on a machine with no model
server: `chokepoint.submit` is currently reached only from `generation/routes.py`, so a change set
otherwise costs a full generation run.

    docker compose exec -T backend python /tmp/submit_host_apply.py <project-id> <user-id> [approve]
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import UserRole
from src.auth.principal import Principal
from src.governance.chokepoint import ChangeItemRequest, MutationRequest
from src.main import create_app

#: The file the change set creates, relative to the workspace root the agent resolves. A forward
#: slash deliberately: a change item's path is a POSIX-style relative path on the wire, and the
#: agent is responsible for turning it into a native path. If that translation were missing, this
#: would land in a directory literally named `deploy/` on Windows, which is the assertion's point.
TARGET = "deploy/Dockerfile"

CONTENT = """FROM node:20-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY . .
EXPOSE 3000
CMD ["node", "src/index.js"]
"""


#: The pre-existing file an `update` overwrites, and the bytes it holds beforehand. The agent must
#: take a timestamped backup before writing, and `old_content` is what makes a stale apply
#: detectable: its SHA-256 becomes `change_items.old_hash` and the agent recomputes it (§6.3).
UPDATE_TARGET = "package.json"

#: The rollback pair. The second path is occupied by a directory, so its write cannot succeed.
ROLLBACK_FIRST = "rollback/first.txt"
ROLLBACK_BLOCKED = "rollback/blocked"
UPDATE_OLD = '{"name":"host-fixture","version":"1.0.0","dependencies":{"express":"4.19.2"}}'
UPDATE_NEW = '{"name":"host-fixture","version":"1.1.0","dependencies":{"express":"4.19.2"}}'


def items(mode: str) -> tuple[ChangeItemRequest, ...]:
    """The change items for each mode this script can submit.

    `create` proves a file lands and an intermediate directory is made. `update` proves the backup:
    it overwrites a file that already exists, which is the only case where there is a pre-image to
    preserve, and `step 11` of the containerised journey asserts the same property from the inside.
    """
    if mode == "rollback":
        # TWO ITEMS, THE SECOND IMPOSSIBLE. `rollback_blocked` names a path where a DIRECTORY already
        # exists, and writing a file over a directory fails on every platform — Windows and Linux
        # both refuse it, for once with the same outcome. So the first item applies, the second
        # cannot, and the agent must undo the first from its backup and report `apply-rolled-back`.
        #
        # A permission bit would NOT work here: `assertOwnerOnly` skips Windows because NTFS ACLs
        # are not the 0600 the POSIX side means, so a read-only file is not a portable way to make a
        # write fail. A directory in the way is.
        return (
            ChangeItemRequest(
                file_path=ROLLBACK_FIRST, action="create", new_content="applied first\n"
            ),
            ChangeItemRequest(
                file_path=ROLLBACK_BLOCKED, action="create", new_content="cannot land\n"
            ),
        )
    if mode == "update":
        return (
            ChangeItemRequest(
                file_path=UPDATE_TARGET,
                action="update",
                old_content=UPDATE_OLD,
                new_content=UPDATE_NEW,
            ),
        )
    return (ChangeItemRequest(file_path=TARGET, action="create", new_content=CONTENT),)


async def main(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    approve: bool,
    mode: str,
    revert_of: uuid.UUID | None = None,
) -> int:
    app = create_app()
    async with app.router.lifespan_context(app):
        maker = app.state.sessionmaker
        chokepoint = app.state.governance_chokepoint

        # REVERT is a mutation and goes through the whole chokepoint again with its own authority
        # (§11.6, D-66): stage 3 compiles the INVERSE change set and every other stage runs over it.
        # APPROVE-ONLY, for a change set that already exists — a reverse set arrives
        # `pending_approval` because a revert is itself a mutation needing authority (§11.6).
        if mode == "approve-only":
            async with maker() as session:  # type: AsyncSession
                principal = await principal_for(session, user_id)
                result = await chokepoint.approve(
                    session, change_set_id=revert_of, principal=principal, comment="host-apply proof"
                )
                await session.commit()
            print(json.dumps({"change_set_id": str(revert_of), "status": result.status}))
            return 0

        # ONE CALL FOR BOTH HALVES OF A REVERT, and the reason is not tidiness.
        #
        # A revert produces a reverse change set that arrives `pending_approval`, and approving it is
        # what mints the envelope and hands it to the hub. Doing those in two separate
        # `docker compose exec` invocations means two short-lived processes, each composing its own
        # `AgentHub`, and the reverse id has to be rediscovered in between by querying for "the newest
        # pending_approval set" — which is a guess about which row is meant. Doing both here reads the
        # id from the object that created it and keeps one process responsible for the whole transit.
        if mode == "revert-and-approve":
            async with maker() as session:  # type: AsyncSession
                principal = await principal_for(session, user_id)
                await chokepoint.revert(session, change_set_id=revert_of, principal=principal)
                await session.commit()

            # `Submission.change_set_id` reports the ORIGINAL for a revert, so the reverse set's id is
            # read from the audit record that names it: `revert` writes `after_state.reverts` naming
            # the original when it authorises the reverse set. That is an exact link rather than an
            # ordering heuristic.
            async with maker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT resource_id FROM audit_events "
                            "WHERE after_state ? 'reverts' "
                            "AND CAST(after_state->>'reverts' AS uuid) = :original "
                            "ORDER BY seq DESC LIMIT 1"
                        ),
                        {"original": revert_of},
                    )
                ).mappings().first()
            if row is None:
                raise SystemExit("the revert left no audit record naming the original")
            reverse_id = uuid.UUID(str(row["resource_id"]))

            async with maker() as session:
                principal = await principal_for(session, user_id)
                approved = await chokepoint.approve(
                    session,
                    change_set_id=reverse_id,
                    principal=principal,
                    comment="host-apply proof",
                )
                await session.commit()
            print(
                json.dumps(
                    {
                        "reverse_change_set_id": str(reverse_id),
                        "status": approved.status,
                        "reverted": str(revert_of),
                    }
                )
            )
            return 0

        if mode == "revert":
            async with maker() as session:  # type: AsyncSession
                principal = await principal_for(session, user_id)
                reverse = await chokepoint.revert(
                    session, change_set_id=revert_of, principal=principal
                )
                await session.commit()
            print(
                json.dumps(
                    {
                        "reverse_change_set_id": str(reverse.change_set_id),
                        "status": reverse.status,
                        "reverted": str(revert_of),
                    }
                )
            )
            return 0

        async with maker() as session:  # type: AsyncSession
            principal = await principal_for(session, user_id)
            submission = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=project_id,
                    items=items(mode),
                    reason="host-apply proof: a change set applied by a host agent binary",
                    origin="manual",
                    environment="dev",
                ),
                principal=principal,
            )
            await session.commit()
            out = {
                "change_set_id": str(submission.change_set_id),
                "status": submission.status,
                "target": UPDATE_TARGET if mode == "update" else (ROLLBACK_FIRST if mode == "rollback" else TARGET),
            }

        if approve:
            async with maker() as session:
                principal = await principal_for(session, user_id)
                # APPROVED THROUGH THE CHOKEPOINT, which is what mints the signed envelope and
                # hands it to the hub. Approving by UPDATE would move the row and deliver nothing.
                await app.state.governance_chokepoint.approve(
                    session,
                    change_set_id=uuid.UUID(out["change_set_id"]),
                    principal=principal,
                    comment="host-apply proof",
                )
                await session.commit()
            out["approved"] = True

        print(json.dumps(out))
    return 0


async def principal_for(session: AsyncSession, user_id: uuid.UUID) -> Principal:
    row = (
        await session.execute(
            text("SELECT idp_subject, email FROM users WHERE id = :id"), {"id": user_id}
        )
    ).mappings().one()
    return Principal.for_user(
        user_id=user_id,
        subject=row["idp_subject"],
        email=row["email"],
        role=UserRole.ADMIN,
    )


def mode_from(args: list[str]) -> str:
    for candidate in ("update", "revert-and-approve", "revert", "approve-only", "rollback"):
        if candidate in args:
            return candidate
    return "create"


def revert_target(args: list[str]) -> uuid.UUID | None:
    """The change set a revert inverts, taken as the argument after `revert`."""
    for verb in ("revert-and-approve", "revert", "approve-only"):
        if verb in args:
            return uuid.UUID(args[args.index(verb) + 1])
    return None


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            main(
                uuid.UUID(sys.argv[1]),
                uuid.UUID(sys.argv[2]),
                "approve" in sys.argv[3:],
                mode_from(sys.argv[3:]),
                revert_target(sys.argv[3:]),
            )
        )
    )
