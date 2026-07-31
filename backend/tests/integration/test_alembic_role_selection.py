# SPDX-License-Identifier: FSL-1.1-ALv2
"""Alembic connects as the migrator role, not as the application role.

design.md §6.4, §13.1; Appendix E criterion 9; tasks.md leaf 5.9.

§6.4 states the two-role arrangement and calls it easy to lose: `DATABASE_URL` is
`forgeops_app`, which cannot UPDATE or DELETE audit rows; `ALEMBIC_DATABASE_URL` is
`forgeops_migrator`, which owns the schema. "A single-role deployment silently defeats
mechanism 3", because the application would then own `audit_events` and could drop the
append-only triggers whenever it liked.

`alembic/env.py` read `DATABASE_URL` and nothing else, so `alembic upgrade head` — the
command `docs/deployment.md` and the Compose entrypoint both run — connected as the
*application* role. `ALEMBIC_DATABASE_URL` was registered in `core/config.py` and
shipped in `.env.example`; no code path read it.

Why every existing migration test passed anyway: `migration_support.run_alembic` set
`DATABASE_URL` to whatever URL the test handed it, and the tests hand it the migrator
URL. Implementation and fixture used the same variable name, so they agreed by
construction and neither could observe the mistake — the same shape as D-58's JWKS
fixtures, which served the guessed path the implementation asked for.

The decisive assertion is the one the old arrangement could not make: give the two
variables DIFFERENT values and prove the migrator's is the one used.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

from .migration_support import BACKEND_DIR

pytestmark = pytest.mark.mandatory


def _alembic(env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    """Run `alembic current` with an explicitly controlled environment."""
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class TestTheMigratorVariableIsRead:
    def test_env_py_reads_alembic_database_url(self) -> None:
        """The source performs the read. A grep-level assertion, deliberately.

        This is the cheapest possible guard against the exact regression: a future edit
        that reverts to `DATABASE_URL` only would pass every schema test in the suite,
        because those tests set both. Reading the source catches it in milliseconds and
        needs no database.

        It matches the READ EXPRESSION, not the bare variable name. Matching the name
        would be satisfied by the comment above it, so a reverted implementation with an
        intact comment would pass — a control that cannot fail is not a control, which is
        the whole subject of §0.4.5.
        """
        source = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
        assert 'os.environ.get("ALEMBIC_DATABASE_URL")' in source, (
            "alembic/env.py does not read ALEMBIC_DATABASE_URL, so `alembic upgrade head` "
            "connects as the application role and the two-role split in design §6.4 is "
            "decorative"
        )

    def test_the_migrator_url_wins_over_the_application_url(self, database_url: str) -> None:
        """With the two variables pointing at different databases, the migrator's wins.

        Proved by pointing `DATABASE_URL` at a database that does not exist: if the
        migrator variable is honoured the command succeeds, and if `DATABASE_URL` is
        honoured it cannot. This is the assertion the previous arrangement made
        impossible, because both names carried the same value.
        """
        unusable = "postgresql+asyncpg://forgeops_app@127.0.0.1:1/does-not-exist"
        result = _alembic({"ALEMBIC_DATABASE_URL": database_url, "DATABASE_URL": unusable})
        assert result.returncode == 0, (
            "alembic failed while ALEMBIC_DATABASE_URL named a working database, so "
            f"DATABASE_URL was used instead:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "ALEMBIC_DATABASE_URL" in result.stderr, (
            f"alembic did not report ALEMBIC_DATABASE_URL as its connection source; stderr was:\n{result.stderr}"
        )

    def test_database_url_remains_the_fallback(self, database_url: str) -> None:
        """A single-role development database still works with `DATABASE_URL` alone."""
        result = _alembic({"ALEMBIC_DATABASE_URL": None, "DATABASE_URL": database_url})
        assert result.returncode == 0, (
            f"alembic failed with DATABASE_URL alone:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "DATABASE_URL" in result.stderr and "ALEMBIC_DATABASE_URL" not in result.stderr, (
            f"alembic did not fall back to DATABASE_URL; stderr was:\n{result.stderr}"
        )


class TestTheDeploymentPathIsConsistent:
    def test_env_example_ships_the_migrator_variable(self) -> None:
        """A variable no example file mentions is a variable no operator sets."""
        example = (pathlib.Path(BACKEND_DIR).parent / ".env.example").read_text(encoding="utf-8")
        assert "ALEMBIC_DATABASE_URL=" in example, (
            ".env.example does not ship ALEMBIC_DATABASE_URL, so a fresh deployment "
            "migrates as the application role by default (design §13.1)"
        )
