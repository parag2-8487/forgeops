# SPDX-License-Identifier: FSL-1.1-ALv2
"""The sharded backend job must still run every test.

WHY THIS EXISTS

`ci.yml`'s `backend` job was split into two shards because `pytest` was 2621s of a 2762s job and 95% of the
whole workflow's critical path. The split is by test DIRECTORY, which is readable and fast — and silently
lossy the moment someone adds a directory.

That is not hypothetical: the first version of the split listed `tests/unit tests/meta tests/property` and
`tests/integration`, and missed `tests/generation` and `tests/secrets` entirely. One file each, so the run
would have gone green with two files fewer than `pytest tests/` collects, and the coverage gate would have
been computed over a smaller suite.

So this asserts the property the split has to preserve: the union of the shards' paths covers every
directory that holds a test. It reads the workflow, not a copy of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TESTS_DIR = REPO_ROOT / "backend" / "tests"


def shard_paths() -> dict[str, list[str]]:
    """`shard -> the paths that shard runs`, read from the workflow."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    include = document["jobs"]["backend"]["strategy"]["matrix"]["include"]
    return {entry["shard"]: str(entry["paths"]).split() for entry in include}


def directories_holding_tests() -> set[str]:
    """Every `backend/tests/<dir>` that contains at least one `test_*.py`, at any depth."""
    found: set[str] = set()
    for path in TESTS_DIR.rglob("test_*.py"):
        relative = path.relative_to(TESTS_DIR)
        if relative.parts:
            found.add(relative.parts[0])
    return found


def test_the_workflow_declares_shards() -> None:
    """Guards the tests below from passing vacuously if the matrix is removed."""
    shards = shard_paths()
    assert len(shards) >= 2, f"the backend job declares {len(shards)} shard(s); this file assumes a split"
    for shard, paths in shards.items():
        assert paths, f"shard {shard!r} runs no paths"


def test_shard_paths_cover_every_test_directory() -> None:
    """The property the split must preserve, and the one the first attempt broke."""
    covered = {path.removeprefix("tests/").split("/")[0] for paths in shard_paths().values() for path in paths}
    missing = sorted(directories_holding_tests() - covered)
    assert not missing, (
        f"these directories hold tests and no shard runs them: {missing}. "
        "The suite would shrink silently and the coverage gate would be computed over less than the whole "
        "suite. Add them to a shard's `paths` in .github/workflows/ci.yml."
    )


def test_no_shard_names_a_path_that_does_not_exist() -> None:
    """A typo'd path collects nothing and pytest exits 0, so the shard passes having run nothing."""
    for shard, paths in shard_paths().items():
        for path in paths:
            assert (REPO_ROOT / "backend" / path).is_dir(), f"shard {shard!r} names {path!r}, which is not a directory"


def test_the_shards_do_not_overlap() -> None:
    """Overlap is not incorrect, but it is wasted wall-clock on the critical path this split exists to cut."""
    seen: dict[str, str] = {}
    for shard, paths in shard_paths().items():
        for path in paths:
            assert path not in seen, f"{path!r} is run by both {seen[path]!r} and {shard!r}"
            seen[path] = shard


@pytest.mark.parametrize("shard", ["unit", "meta", "integration"])
def test_each_shard_still_exists_by_name(shard: str) -> None:
    """The names are referenced by the coverage artefact pattern and by the combine job's count guard.

    `meta` was added after the first split proved slower than the job it replaced: it runs the entire
    mutation harness against the real tree, 54 minutes of a 65-minute shard locally.
    """
    assert shard in shard_paths()


def test_the_combine_job_requires_one_file_per_shard() -> None:
    """The count guard in `backend-coverage` must match the number of shards.

    A guard that is lower than the shard count would let the 70% gate be computed over a partial suite,
    which is the one thing the split must not do.
    """
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = document["jobs"]["backend-coverage"]["steps"]
    combine = next(s for s in steps if "combine" in (s.get("name") or "").lower())
    expected = len(shard_paths())
    assert f"-lt {expected} " in combine["run"], (
        f"the combine step does not require {expected} coverage files, one per shard. Its script is:\n" + combine["run"]
    )
