# SPDX-License-Identifier: FSL-1.1-ALv2
"""`make verify-chain` — recompute the audit hash chain from the command line (§11.9, §13.4).

Why a CLI as well as `GET /api/v1/audit/verify`
----------------------------------------------
They answer the same question in two situations that do not overlap. The route is the product
feature: an admin can check the chain without a database shell, which is what makes tamper
evidence usable rather than theoretical. This CLI runs when the API is the thing under suspicion —
after a restore, during an incident, or when the answer must not come from the process being
audited. An integrity check that can only be obtained from the service whose integrity is in
question is not much of a check.

It exits non-zero on a divergence, so it can gate a script. It prints the first divergent `seq`
and nothing else about the row: the point is to say *where to look*, and dumping the tampered
content into a terminal or a CI log is how audit content ends up somewhere with weaker access
control than the table it came from.

`--require-rows`, and why a zero-row chain is OK for an operator and not OK for a gate
---------------------------------------------------------------------------------------
Run against a fresh database this command printed `OK - 0 row(s)` and exited 0. For an operator
that is the truth: an empty chain has nothing that fails to reproduce. For a CI step gating on the
exit code it is the vacuity trap §0.4.5 closes for the mutation harness and §0.4.4 closes for the
mandatory selection, arriving in a §13.4 operator command — a green step that verified nothing,
indistinguishable from a green step that verified the chain.

Rather than change what the command means, the caller states its expectation: `--require-rows N`
fails when fewer than N rows were checked. The default is 0, so `make verify-chain` behaves exactly
as before, and `compose-smoke` passes `--require-rows 1` after writing records, so its green is a
statement about a chain that exists (D-69).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.config import get_settings
from ..core.db import create_db_engine
from .writer import AuditWriter


async def _run(tenant: uuid.UUID | None, since: int, require_rows: int) -> int:
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        writer = AuditWriter(advisory_lock_key=settings.audit_advisory_lock_key)
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=since)
    except ProgrammingError as exc:
        # Against a database that has never been migrated this used to surface as a 40-line
        # SQLAlchemy traceback ending in `UndefinedTableError`. That is a true statement and a
        # useless one: the reader's next action is `alembic upgrade head`, and nothing in the
        # traceback says so. An integrity tool whose first-run output looks like a crash is a tool
        # an operator stops trusting before it has told them anything.
        if "audit_events" in str(exc) and "does not exist" in str(exc):
            print(
                "verify-chain: ERROR - the audit_events table does not exist in this database. "
                "The schema has not been migrated; run `alembic upgrade head` first.",
                file=sys.stderr,
            )
            return 2
        raise
    finally:
        await engine.dispose()

    scope = "the untenanted chain" if tenant is None else f"tenant {tenant}"
    if result.ok:
        # ASCII only. This string lands in CI logs, in `docker compose run` output and on a
        # Windows console whose code page is not UTF-8; a dash that renders as a mojibake byte in
        # one of those is a diagnostic nobody can grep for.
        print(f"verify-chain: OK - {result.rows_checked} row(s) of {scope} reproduce their stored hashes")
        if result.rows_checked < require_rows:
            print(
                f"verify-chain: ERROR - {result.rows_checked} row(s) checked, fewer than the "
                f"--require-rows {require_rows} the caller demanded. An OK over fewer rows than "
                "expected is not the evidence that was asked for, and over zero rows it is no "
                "evidence at all: an empty chain reproduces trivially.",
                file=sys.stderr,
            )
            return 1
        return 0
    divergence = result.divergence
    assert divergence is not None  # `ok` is False exactly when this is set
    print(
        f"verify-chain: DIVERGENCE at seq {divergence.seq} in {scope} "
        f"({divergence.kind}: {divergence.detail}) after {result.rows_checked} verified row(s)",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify-chain", description=__doc__)
    parser.add_argument(
        "--tenant",
        type=uuid.UUID,
        default=None,
        help="tenant UUID; omitted verifies the untenanted chain, which is its own chain (D-35)",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=0,
        help="recompute from this seq onward; 0 starts at genesis (Appendix A.8)",
    )
    parser.add_argument(
        "--require-rows",
        type=int,
        default=0,
        metavar="N",
        help=(
            "fail unless at least N rows were checked. Default 0 keeps an operator run truthful "
            "about an empty chain; a gate should pass 1 or more so its green is not vacuous"
        ),
    )
    args = parser.parse_args(argv)
    if args.since < 0:
        parser.error("--since must be zero or greater")
    if args.require_rows < 0:
        parser.error("--require-rows must be zero or greater")
    return asyncio.run(_run(args.tenant, args.since, args.require_rows))


if __name__ == "__main__":
    raise SystemExit(main())
