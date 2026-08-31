# SPDX-License-Identifier: FSL-1.1-ALv2
"""The migration chain is linear and reaches `0001_initial`.

design.md §6.4, §6.5; tasks.md leaf 5.9.

Alembic tolerates branches. A branched history still upgrades, still reports success,
and produces a *different schema depending on the order the branches merge* — which is
exactly the kind of environment-dependent outcome §6.5's "linear, no branches" rule
exists to forbid. `alembic heads` returning two rows is the only cheap signal, and
nothing in the tool fails on it by default.

This test also reads the revision graph directly rather than only shelling out, so it
can say *which* revision broke linearity instead of only that something did.
"""

from __future__ import annotations

import pathlib

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from .migration_support import BACKEND_DIR, alembic_ok, run_alembic

pytestmark = pytest.mark.mandatory

FIRST_REVISION = "0001"
#: The current head, asserted rather than discovered so a stray revision file breaks the build.
#:
#: `0009` until leaf 7.5. Advanced to `0010` by D-63, which reconciles `change_sets.status` with
#: design §3.6 — §6.5's revision plan stops at `0009`, so this constant moving is the reviewable
#: signal that a revision beyond the plan was added deliberately rather than by accident.
#:
#: `0011` admits `pending` to `generation_runs.served_from`, so a run still in flight stops claiming
#: it was served from a template before the pipeline had run. `0012` gives `embeddings_local` the
#: cAST metadata columns `embeddings` has carried since `0003`; without them the first scan under
#: the self-hosted embedding backend failed on `column "symbol" does not exist` after computing
#: every vector, and a project on that backend would have had permanently invisible symbols.
#:
#: `0013` adds `projects.archived_at` and `project_favourites`, for PRD FR-05 (archive and delete)
#: and FR-03 (favourites) — both P0 with no route on either side. Favourites needed a TABLE because
#: `projects.settings.favourite`, which has existed since `0009`, is per project: in a tenant with
#: two people one person starring a project would reorder the other's list. Tags needed nothing,
#: because `project_tags` was created by `0009` and nothing had ever written to it.
EXPECTED_HEAD = "0015"


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


class TestTheGraphIsLinear:
    def test_there_is_exactly_one_head(self) -> None:
        heads = _script_directory().get_heads()
        assert len(heads) == 1, (
            f"the revision graph has {len(heads)} heads {sorted(heads)}; a branched "
            f"history still upgrades successfully and produces a different schema "
            f"depending on merge order (design §6.5)"
        )

    def test_the_head_is_the_expected_revision(self) -> None:
        assert list(_script_directory().get_heads()) == [EXPECTED_HEAD]

    def test_no_revision_has_more_than_one_parent(self) -> None:
        """A merge revision is the other shape a branch takes."""
        offenders = []
        for script in _script_directory().walk_revisions():
            parents = script.down_revision
            if isinstance(parents, tuple | list) and len(parents) > 1:
                offenders.append((script.revision, tuple(parents)))
        assert not offenders, offenders

    def test_no_revision_has_more_than_one_child(self) -> None:
        """The forward-facing half. Two revisions naming the same `down_revision` is a
        fork even when `alembic heads` has been merged back together since."""
        children: dict[str, list[str]] = {}
        for script in _script_directory().walk_revisions():
            parent = script.down_revision
            if isinstance(parent, str):
                children.setdefault(parent, []).append(script.revision)
        forks = {parent: kids for parent, kids in children.items() if len(kids) > 1}
        assert not forks, forks

    def test_every_chain_reaches_the_initial_revision(self) -> None:
        directory = _script_directory()
        walked = [script.revision for script in directory.walk_revisions()]
        assert walked[-1] == FIRST_REVISION, walked
        # And the walk is exactly as long as the number of revision files, so no
        # revision is unreachable from the head.
        all_revisions = {script.revision for script in directory.get_revisions("heads")}
        assert all_revisions
        files = sorted(
            path.name for path in (BACKEND_DIR / "alembic" / "versions").glob("*.py") if not path.name.startswith("_")
        )
        assert len(walked) == len(files), (
            f"walked {len(walked)} revisions but there are {len(files)} revision files "
            f"{files}; a file not on the chain from the head is dead code that will "
            f"never run"
        )

    def test_the_revision_ids_are_the_zero_padded_sequence(self) -> None:
        """§6.5 fixes the naming as `NNNN_snake_case_summary`. An out-of-sequence id
        makes the history unreadable at a glance, which is the only reason the
        convention exists."""
        walked = [script.revision for script in _script_directory().walk_revisions()]
        assert sorted(walked) == [f"{n:04d}" for n in range(1, len(walked) + 1)], walked


class TestTheCliAgrees:
    """The graph assertions above read Alembic's own API. These run the command an
    operator runs, because a `script_location` or `version_locations` mistake can make
    the two disagree."""

    def test_alembic_heads_reports_one_head(self, database_url: str) -> None:
        result = alembic_ok(database_url, "heads")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1, result.stdout
        assert lines[0].startswith(EXPECTED_HEAD), result.stdout

    def test_alembic_branches_reports_nothing(self, database_url: str) -> None:
        result = run_alembic(database_url, "branches")
        assert result.returncode == 0, result.stderr
        assert not result.stdout.strip(), f"alembic reports branches:\n{result.stdout}"

    def test_the_full_chain_upgrades_and_downgrades(self, schema_at_head: str) -> None:
        """`schema_at_head` already ran `downgrade base` then `upgrade head`. This adds
        the return leg, which is what catches a revision whose `downgrade` was never
        written or never tried."""
        alembic_ok(schema_at_head, "downgrade", "base")
        alembic_ok(schema_at_head, "upgrade", "head")

    def test_every_revision_file_is_importable_python(self) -> None:
        """Two revisions in the versions directory carry `import` statements from
        `src.*` so their check constraints are generated from the model's own tuples.
        A missing `prepend_sys_path` would break that only at migration time."""
        versions = BACKEND_DIR / "alembic" / "versions"
        assert versions.is_dir()
        found = sorted(path.name for path in versions.glob("[0-9]*.py"))
        assert len(found) >= 9, found
        for name in found:
            source = (versions / name).read_text(encoding="utf-8")
            assert "revision: str" in source, name
            assert "down_revision" in source, name
            compile(source, str(pathlib.Path(name)), "exec")
