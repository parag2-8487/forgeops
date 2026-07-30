#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Assert the running application's database role cannot rewrite the audit log.

design.md §6.4, §6.7, §11.9; Appendix E criterion 9; tasks.md leaf 5.6.

§6.4 states the two-role arrangement explicitly "because it is easy to lose":
`DATABASE_URL` connects as `forgeops_app`, which cannot UPDATE or DELETE audit rows,
and `ALEMBIC_DATABASE_URL` connects as `forgeops_migrator`, which owns the schema. A
single-role deployment silently defeats mechanism 3 — every trigger is still there,
every test still passes, and the application can drop the triggers whenever it likes,
because dropping a trigger needs the table ownership a merged role would hold.

This check exists because that failure is invisible from inside the application. It
connects with the *configured* URL and interrogates the privileges of whatever role
that URL actually authenticates as, rather than the role the configuration claims.

Usage:
    check-db-roles.py [--url URL]

The URL defaults to `$DATABASE_URL`, then `$FORGEOPS_TEST_DATABASE_URL`. Exit 0 when
the role is correctly constrained, 1 when it is not, and 2 when no URL was supplied
or the database is unreachable — never a silent pass.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

#: The audit log must be append-only for the application role.
FORBIDDEN_ON_AUDIT = ("UPDATE", "DELETE", "TRUNCATE")
#: And it must still be able to append and read, or §1.9 cannot be delivered at all.
REQUIRED_ON_AUDIT = ("INSERT", "SELECT")
AUDIT_TABLE = "audit_events"


def _normalise(url: str) -> str:
    """asyncpg needs a bare postgres URL, not a SQLAlchemy dialect URL."""
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


async def check(url: str) -> list[str]:
    try:
        import asyncpg
    except ImportError:  # pragma: no cover - asyncpg is a hard dependency
        raise SystemExit("FAIL: asyncpg is required by this check") from None

    connection = await asyncpg.connect(_normalise(url))
    failures: list[str] = []
    try:
        role = await connection.fetchval("SELECT current_user")
        is_superuser = await connection.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        )
        print(f"connected as role: {role}")

        table_exists = await connection.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = $1",
            AUDIT_TABLE,
        )
        if not table_exists:
            failures.append(
                f"{AUDIT_TABLE} does not exist; run `alembic upgrade head` before this check. "
                f"A missing table is reported as a failure rather than skipped, because "
                f"'nothing to check' and 'checked and correct' must not look alike."
            )
            return failures

        # A superuser bypasses every privilege check in PostgreSQL, so if the
        # application connects as one, mechanism 3's REVOKE half is inert no matter
        # what the grants say. That is the single most likely way to defeat this
        # design accidentally, and it is reported first.
        if is_superuser:
            failures.append(
                f"the application role {role!r} is a SUPERUSER, which bypasses every "
                f"privilege check: the REVOKE half of the audit immutability design "
                f"(§6.4 mechanism 3) has no effect. Connect as forgeops_app."
            )

        for privilege in FORBIDDEN_ON_AUDIT:
            held = await connection.fetchval(
                "SELECT has_table_privilege(current_user, $1, $2)", AUDIT_TABLE, privilege
            )
            if held:
                failures.append(
                    f"role {role!r} holds {privilege} on {AUDIT_TABLE}; §6.4 mechanism 3 "
                    f"requires it revoked from the application role"
                )
            else:
                print(f"ok:   {privilege} on {AUDIT_TABLE} is not held")

        for privilege in REQUIRED_ON_AUDIT:
            held = await connection.fetchval(
                "SELECT has_table_privilege(current_user, $1, $2)", AUDIT_TABLE, privilege
            )
            if not held:
                failures.append(
                    f"role {role!r} lacks {privilege} on {AUDIT_TABLE}; the application "
                    f"must still be able to append and read the log (§11.9)"
                )
            else:
                print(f"ok:   {privilege} on {AUDIT_TABLE} is held")

        # Ownership is the other route to rewriting history: an owner can drop the
        # triggers, and then UPDATE is only a GRANT away.
        owner = await connection.fetchval(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = $1",
            AUDIT_TABLE,
        )
        if owner == role:
            failures.append(
                f"role {role!r} OWNS {AUDIT_TABLE}; an owner can drop the immutability "
                f"triggers, so the REVOKE is decorative. Migrations must run as "
                f"forgeops_migrator and the application as forgeops_app (§6.7)."
            )
        else:
            print(f"ok:   {AUDIT_TABLE} is owned by {owner!r}, not by the application role")

        triggers = await connection.fetch(
            "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = $1 AND NOT t.tgisinternal",
            AUDIT_TABLE,
        )
        names = {record["tgname"] for record in triggers}
        for expected in (
            "trg_audit_events_no_update",
            "trg_audit_events_no_delete",
            "trg_audit_events_no_truncate",
        ):
            if expected not in names:
                failures.append(f"trigger {expected} is missing from {AUDIT_TABLE}")
        if not failures:
            print(f"ok:   all three immutability triggers present: {sorted(names)}")
    finally:
        await connection.close()

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="database URL; defaults to $DATABASE_URL then $FORGEOPS_TEST_DATABASE_URL",
    )
    args = parser.parse_args()

    url = (
        args.url
        or os.environ.get("DATABASE_URL")
        or os.environ.get("FORGEOPS_TEST_DATABASE_URL")
        or ""
    ).strip()
    if not url:
        print(
            "FAIL: no database URL. Pass --url or set DATABASE_URL. This is exit 2, not "
            "a skip: a check that passes when it cannot run is worse than no check.",
            file=sys.stderr,
        )
        return 2

    try:
        failures = asyncio.run(check(url))
    except OSError as exc:
        print(f"FAIL: could not reach the database: {exc}", file=sys.stderr)
        return 2

    if failures:
        print("FAIL: the application database role is not correctly constrained:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("OK: the application role cannot rewrite the audit log (design §6.4, §6.7)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
