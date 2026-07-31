#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Seed the audit chain and prove `verify-chain` both passes and FAILS (design §11.9, §13.4).

Why this exists
---------------
`make verify-chain` against a fresh stack printed `verify-chain: OK - 0 row(s)` and exited 0. Every
word of that is true and it is worth nothing as evidence: a CI step gating on the exit code would
have been green over an empty table, which is §0.4.5's `VACUOUS` row and §0.4.4's empty selection
arriving in an operator command (D-69).

So this script makes the end-to-end claim a real one, in three parts, and the third is the part
that matters:

1. **Write** records through the real `AuditWriter` as the application role, in one transaction, so
   the chain is produced the way production produces it — not by INSERT statements that could agree
   with the verifier by construction.
2. **Verify** and require a non-empty chain. This is the half a naive smoke step would stop at.
3. **Tamper one row and verify again**, asserting the divergence is reported at exactly the seq
   that was altered. Without this the step proves the command runs, not that it can tell. A
   verifier that returned `ok` unconditionally would pass parts 1 and 2.

Then it restores the row and re-verifies, so the stack is left with an intact chain and the script
is re-runnable.

The tamper is performed as the TABLE OWNER with `0007`'s UPDATE trigger disabled, which is
`0007`'s own threat model: the application role cannot do this — `test_0007_audit.py` asserts the
REVOKE — so the only actor who can is one who already owns the schema. Simulating the attack any
other way would be simulating an attack the database already prevents.

Invocation, from the repository root, against a stack whose schema is at head:

    docker compose run --rm backend python -m scripts.audit_chain_smoke   # not importable that way
    docker compose run --rm -v "$PWD/scripts:/scripts:ro" backend python /scripts/audit-chain-smoke.py

The second form is what `compose-smoke` uses: the script is mounted rather than baked into the
image, because it is test scaffolding and the runtime image should not ship it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, "/app")

from src.audit.writer import AuditDraft, AuditWriter  # noqa: E402

#: How many records to write. Three is the smallest number that exercises a chain rather than a
#: pair: genesis, a middle row whose prev_hash points at a real predecessor, and a successor to
#: the row the tamper targets, so "the FIRST divergence" has something after it to not report.
RECORD_COUNT = 3

#: The row to tamper. The middle one, so a verifier that only ever reported the last row, or only
#: the first, would be caught.
TAMPER_INDEX = 1


def _fail(message: str) -> None:
    print(f"audit-chain-smoke: FAIL - {message}", file=sys.stderr)
    raise SystemExit(1)


async def main() -> int:
    app_url = os.environ.get("DATABASE_URL", "")
    owner_url = os.environ.get("ALEMBIC_DATABASE_URL", "")
    if not app_url or not owner_url:
        _fail("DATABASE_URL and ALEMBIC_DATABASE_URL must both be set; the tamper needs the owner")

    tenant = uuid.uuid4()
    app_engine = create_async_engine(app_url, pool_pre_ping=True)
    owner_engine = create_async_engine(owner_url, pool_pre_ping=True)
    writer = AuditWriter()

    try:
        sessions = async_sessionmaker(app_engine, expire_on_commit=False, autoflush=False)

        # ── 1. write, through the real writer, in one transaction ──────────────────────────
        written: list[int] = []
        async with sessions() as session:
            for index in range(RECORD_COUNT):
                event = await writer.append(
                    session,
                    AuditDraft(
                        action="compose.smoke",
                        resource_kind="audit_chain",
                        resource_id=f"smoke-{index}",
                        reason=f"compose-smoke record {index}: proves the chain is non-empty",
                        outcome="allowed",
                        actor_kind="system",
                        tenant_id=tenant,
                        after_state={"index": index},
                    ),
                )
                written.append(event.seq)
            await session.commit()
        print(f"audit-chain-smoke: wrote {len(written)} record(s) at seq {written}")

        # ── 2. verify, and require the chain to be non-empty ───────────────────────────────
        async with sessions() as session:
            clean = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        if not clean.ok:
            _fail(f"a freshly written chain did not verify: {clean.divergence}")
        if clean.rows_checked != RECORD_COUNT:
            _fail(f"verified {clean.rows_checked} row(s), wrote {RECORD_COUNT}")
        print(f"audit-chain-smoke: OK - {clean.rows_checked} freshly written row(s) reproduce their hashes")

        # ── 3. the control: tamper, and require the verifier to object at the right seq ────
        target = written[TAMPER_INDEX]
        async with owner_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(
                    text("UPDATE audit_events SET reason = :r WHERE seq = :s"),
                    {"r": "a reason nobody gave", "s": target},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))

        async with sessions() as session:
            tampered = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        if tampered.ok:
            _fail(
                f"seq {target} was altered and verify_chain still reported OK. The chain check is "
                "decorative, which is the one outcome this script exists to make impossible"
            )
        if tampered.divergence is None or tampered.divergence.seq != target:
            _fail(f"divergence reported at {tampered.divergence and tampered.divergence.seq}, altered {target}")
        if tampered.divergence.kind != "hash":
            _fail(f"divergence kind is {tampered.divergence.kind!r}, expected 'hash' for an edited field")
        print(
            f"audit-chain-smoke: CONTROL BITES - altering seq {target} is reported as "
            f"{tampered.divergence.kind} at seq {tampered.divergence.seq}"
        )

        # ── restore, so the stack is left with an intact chain and this is re-runnable ─────
        async with owner_engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_no_update"))
            try:
                await conn.execute(
                    text("UPDATE audit_events SET reason = :r WHERE seq = :s"),
                    {"r": f"compose-smoke record {TAMPER_INDEX}: proves the chain is non-empty", "s": target},
                )
            finally:
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_no_update"))

        async with sessions() as session:
            restored = await writer.verify_chain(session, tenant_id=tenant, since_seq=0)
        if not restored.ok:
            _fail(f"the chain did not verify after the tamper was reverted: {restored.divergence}")
        print(f"audit-chain-smoke: PASS - chain restored and re-verified over {restored.rows_checked} row(s)")
        return 0
    finally:
        await app_engine.dispose()
        await owner_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
