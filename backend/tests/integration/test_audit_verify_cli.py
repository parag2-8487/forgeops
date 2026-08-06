# SPDX-License-Identifier: FSL-1.1-ALv2
"""`make verify-chain`'s CLI against a REAL PostgreSQL (design.md §11.9, §13.4; D-69).

Why this file exists at all
--------------------------
`src/audit/verify_cli.py` shipped with leaf 7.6 and had **no tests**. It is the command §13.4 puts
in an operator's hands during an incident, and the first time it was run end to end — against the
compose stack, for group 7's close-out — two things were wrong with it, both of which a test would
have caught the day it landed:

* over a fresh database it printed ``verify-chain: OK - 0 row(s)`` and exited **0**. Truthful, and
  worth nothing as evidence. A CI step gating on that exit code is green over an empty table,
  which is §0.4.5's ``VACUOUS`` row and §0.4.4's empty selection arriving in an operator command;
* over an UNMIGRATED database it exited with a forty-line SQLAlchemy traceback ending in
  ``UndefinedTableError``. Also true, also useless: the reader's next action is
  ``alembic upgrade head`` and nothing in the traceback says so.

Why these are integration tests
-------------------------------
The CLI's whole job is to talk to a database, and every clause below is a statement about what it
does when it gets there — including the two failure paths, which are *database* conditions. A unit
test would have to replace the engine, and §0.4.1 forbids the substitute collaborator that makes
the test agree with itself. `main()` is driven exactly as a shell drives it, argv and exit code.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.audit import verify_cli
from src.audit.writer import AuditDraft, AuditWriter

from .migration_support import head_engine, schema_at_head  # noqa: F401 - fixtures

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

RECORD_COUNT = 3


@pytest_asyncio.fixture()
async def sessions(head_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:  # noqa: F811
    return async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture()
async def populated_tenant(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A tenant whose chain holds exactly RECORD_COUNT rows, written by the real writer."""
    tenant = uuid.uuid4()
    writer = AuditWriter()
    async with sessions() as session:
        for index in range(RECORD_COUNT):
            await writer.append(
                session,
                AuditDraft(
                    action="verify_cli_test",
                    resource_kind="audit_chain",
                    resource_id=f"row-{index}",
                    reason="a stated reason, because NFR-14 requires one",
                    outcome="allowed",
                    tenant_id=tenant,
                ),
            )
        await session.commit()
    return tenant


#: The URL a real invocation reads. Taken from the environment verbatim rather than rendered back
#: out of the fixture's `Engine.url`, for two reasons. It is the same string the integration
#: environment already provides, so nothing is reconstructed and nothing can be reconstructed
#: wrongly. And rendering a `URL` into something connectable requires asking SQLAlchemy to include
#: the credential, via a keyword whose name is one of the high-risk phrases the mandatory pre-push
#: scan in `.antigravity/steering/secret-safety.md` blocks on. It was right to block: a line that unmasks
#: a credential is exactly the shape that gate looks for, even when the value is a local test
#: password. Reading the variable avoids the construct instead of excusing it.
#:
#: The first draft of this comment named the keyword, and the scan blocked on the COMMENT — a
#: description of the pattern is indistinguishable from the pattern to anything that greps. Same
#: shape as the source scans in `tests/meta/test_check_no_skips.py`, which flagged the paragraphs
#: explaining the defect they were written to catch.
TEST_DATABASE_URL_VAR = "FORGEOPS_TEST_DATABASE_URL"


def _database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_VAR, "")
    if not url:
        pytest.skip(f"{TEST_DATABASE_URL_VAR} is not set")
    return url


def _url_for_database(name: str) -> str:
    """The same URL, pointed at a different database on the same cluster.

    The database is the last path segment, so this replaces it without parsing or re-rendering
    credentials.
    """
    base, _, _ = _database_url().rpartition("/")
    return f"{base}/{name}"


@pytest.fixture(autouse=True)
def _cli_reads_the_test_database(monkeypatch: pytest.MonkeyPatch, head_engine: AsyncEngine) -> None:  # noqa: F811
    """Point the CLI's settings at the same database the fixtures wrote to.

    The CLI builds its own engine from settings on purpose — it exists for the case where the
    running service is the thing under suspicion — so the test cannot hand it a session. It sets
    the environment variable a real invocation would read. That is configuration, not a substitute
    collaborator: the engine, the driver and the database are all the real ones. `get_settings` is
    uncached, so setting the variable is sufficient and nothing has to be invalidated.

    `head_engine` is requested but not read: it is what applies the schema, and the URL below
    addresses the database it migrated.
    """
    monkeypatch.setenv("DATABASE_URL", _database_url())


async def run_cli(*argv: str) -> int:
    """Invoke `main()` exactly as a shell does, from inside an async test.

    In a thread, because `main()` calls `asyncio.run()` — correctly, since it is a program entry
    point — and `asyncio.run` refuses to nest inside a running loop. Calling the inner coroutine
    directly would skip argument parsing and the exit-code mapping, which are half of what a CLI
    is; a thread keeps argv and the return value on the real path.
    """
    return await asyncio.to_thread(verify_cli.main, list(argv))


class TestAnEmptyChainIsTruthfulButNotEvidence:
    async def test_an_empty_chain_verifies_ok_by_default(self) -> None:
        """Unchanged operator behaviour: nothing to reproduce is not a failure."""
        assert await run_cli("--tenant", str(uuid.uuid4())) == 0

    async def test_an_empty_chain_fails_require_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The clause that stops a gate being green over an empty table (D-69)."""
        assert await run_cli("--tenant", str(uuid.uuid4()), "--require-rows", "1") == 1
        assert "0 row(s) checked" in capsys.readouterr().err

    async def test_require_rows_zero_is_the_default_and_changes_nothing(self) -> None:
        assert await run_cli("--tenant", str(uuid.uuid4()), "--require-rows", "0") == 0


class TestAPopulatedChain:
    async def test_it_verifies_and_reports_the_row_count(
        self, populated_tenant: uuid.UUID, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert await run_cli("--tenant", str(populated_tenant), "--require-rows", "1") == 0
        out = capsys.readouterr().out
        assert f"{RECORD_COUNT} row(s)" in out, out
        assert "OK" in out

    async def test_require_rows_above_the_real_count_fails(self, populated_tenant: uuid.UUID) -> None:
        """The guard is a comparison, not a non-empty check.

        Asserted with a real count so that a `require_rows` implemented as `rows_checked > 0`
        would pass the test above and fail here. Without this clause the flag would be
        indistinguishable from a boolean.
        """
        assert await run_cli("--tenant", str(populated_tenant), "--require-rows", str(RECORD_COUNT + 1)) == 1

    async def test_require_rows_equal_to_the_real_count_passes(self, populated_tenant: uuid.UUID) -> None:
        """The boundary, on the passing side, so the comparison is not off by one."""
        assert await run_cli("--tenant", str(populated_tenant), "--require-rows", str(RECORD_COUNT)) == 0


class TestArgumentRefusals:
    async def test_a_negative_since_is_refused(self) -> None:
        with pytest.raises(SystemExit) as raised:
            verify_cli.main(["--since", "-1"])
        assert raised.value.code == 2

    async def test_a_negative_require_rows_is_refused(self) -> None:
        with pytest.raises(SystemExit) as raised:
            verify_cli.main(["--require-rows", "-1"])
        assert raised.value.code == 2


class TestAnUnmigratedDatabase:
    async def test_it_names_the_missing_table_instead_of_raising(
        self,
        head_engine: AsyncEngine,  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A real database that genuinely lacks the table, not a simulated error.

        The target is the cluster's `postgres` maintenance database, which every PostgreSQL has,
        which the test role can connect to, and in which `audit_events` has never existed. Nothing
        is created or dropped — the only statement issued against it is the SELECT that fails,
        which is the whole point.

        Raising `ProgrammingError` by hand would prove the `except` clause matches a string this
        test wrote, which is the fixture-shaped-around-the-implementation defect (pattern F). Only
        a real unmigrated database proves the branch fires on the driver's own error.
        """
        monkeypatch.setenv("DATABASE_URL", _url_for_database("postgres"))
        assert await run_cli() == 2
        err = capsys.readouterr().err
        assert "audit_events" in err
        assert "alembic upgrade head" in err
        assert "Traceback" not in err
