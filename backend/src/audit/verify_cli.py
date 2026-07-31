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
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..core.config import get_settings
from ..core.db import create_db_engine
from .writer import AuditWriter


async def _run(tenant: uuid.UUID | None, since: int) -> int:
    settings = get_settings()
    engine = create_db_engine(settings)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        writer = AuditWriter(advisory_lock_key=settings.audit_advisory_lock_key)
        async with sessions() as session:
            result = await writer.verify_chain(session, tenant_id=tenant, since_seq=since)
    finally:
        await engine.dispose()

    scope = "the untenanted chain" if tenant is None else f"tenant {tenant}"
    if result.ok:
        # ASCII only. This string lands in CI logs, in `docker compose run` output and on a
        # Windows console whose code page is not UTF-8; a dash that renders as a mojibake byte in
        # one of those is a diagnostic nobody can grep for.
        print(f"verify-chain: OK - {result.rows_checked} row(s) of {scope} reproduce their stored hashes")
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
    args = parser.parse_args(argv)
    if args.since < 0:
        parser.error("--since must be zero or greater")
    return asyncio.run(_run(args.tenant, args.since))


if __name__ == "__main__":
    raise SystemExit(main())
