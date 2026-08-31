# SPDX-License-Identifier: FSL-1.1-ALv2
"""Revision `0009_project_tags_and_settings`.

design.md §6.5, §11.3; tasks.md leaf 5.8.

The settings validation is Python, not a check constraint per key, and the reason is
worth recording: a constraint per key would make every new setting a migration, and
one constraint over a whole JSONB document would be an unreadable predicate that
still could not say "must be a list of strings". What the schema *can* usefully
narrow is that `settings` is a JSON object rather than an array or a scalar, so that
is what `ck_projects_settings_is_object` does — a real narrowing that cannot rot.

`validate_project_settings` refuses an unknown key rather than dropping it. Silently
discarding a key the caller believed they set is how a project ends up with settings
nobody can account for, and in the `embedding_backend` case it would mean a project
whose vectors are quietly in the wrong table (D-48).

The validator tests need no database and are in their own class, so they still run —
and still fail — when Postgres is unavailable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from src.projects.models import (
    EMBEDDING_BACKENDS,
    PROJECT_SETTINGS_KEYS,
    ProjectSettingsError,
    validate_project_settings,
)

from .migration_support import make_project, scalar

pytestmark = pytest.mark.mandatory

INSERT_TAG = text("INSERT INTO project_tags (id, project_id, tag) VALUES (:id, :project_id, :tag)")


class TestTheSettingsValidator:
    """No database required, so these run everywhere."""

    def test_an_unknown_embedding_backend_is_rejected(self) -> None:
        """The leaf's named assertion."""
        with pytest.raises(ProjectSettingsError) as caught:
            validate_project_settings({"embedding_backend": "openai"})
        assert "embedding_backend" in str(caught.value)

    @pytest.mark.parametrize("backend", sorted(EMBEDDING_BACKENDS))
    def test_each_declared_backend_is_accepted(self, backend: str) -> None:
        assert validate_project_settings({"embedding_backend": backend})

    def test_an_unknown_key_is_rejected_and_named(self) -> None:
        with pytest.raises(ProjectSettingsError) as caught:
            validate_project_settings({"embeding_backend": "voyage"})
        assert "embeding_backend" in str(caught.value), (
            "the error must name the offending key; a typo the caller cannot see is "
            "the failure this validator exists to surface"
        )

    def test_a_typo_is_not_silently_dropped(self) -> None:
        """States the design choice executably. Returning a cleaned copy was rejected."""
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({"favorite": True})  # American spelling

    def test_every_allowed_key_together_is_accepted(self) -> None:
        settings = {
            "embedding_backend": "bge_m3",
            "llm_budget_usd_month": 25.5,
            "favourite": True,
            "auto_approve_readme_only": False,
            "max_file_size_bytes": 1_048_576,
            "ignore_globs": ["**/node_modules/**", "*.min.js"],
            # Written by the GitHub import (FR-01). Declared rather than written past the validator,
            # because an import that bypassed it would be the only writer in the system allowed to.
            "repo_default_branch": "main",
            "repo_private": True,
            "repo_languages": ["Go", "Python"],
        }
        assert set(settings) == set(PROJECT_SETTINGS_KEYS)
        assert validate_project_settings(settings) == settings

    def test_an_empty_document_is_accepted(self) -> None:
        assert validate_project_settings({}) == {}

    @pytest.mark.parametrize("value", ["25", None, [1]])
    def test_a_non_numeric_budget_is_rejected(self, value: object) -> None:
        if value is None:
            # `None` means "unset", which is legitimate.
            assert validate_project_settings({"llm_budget_usd_month": None}) is not None
            return
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({"llm_budget_usd_month": value})

    def test_a_boolean_budget_is_rejected(self) -> None:
        """`bool` is a subclass of `int` in Python, so `isinstance(True, int)` is true
        and a naive numeric check would accept `llm_budget_usd_month=True`."""
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({"llm_budget_usd_month": True})

    def test_a_negative_budget_is_rejected(self) -> None:
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({"llm_budget_usd_month": -1})

    @pytest.mark.parametrize("flag", ["favourite", "auto_approve_readme_only"])
    @pytest.mark.parametrize("value", ["yes", 1, 0, []])
    def test_a_non_boolean_flag_is_rejected(self, flag: str, value: object) -> None:
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({flag: value})

    @pytest.mark.parametrize("value", [0, -1, "1024", 1.5, True])
    def test_an_invalid_max_file_size_is_rejected(self, value: object) -> None:
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({"max_file_size_bytes": value})

    @pytest.mark.parametrize("value", ["*.js", [1, 2], ["ok", 3], {}])
    def test_invalid_ignore_globs_are_rejected(self, value: object) -> None:
        with pytest.raises(ProjectSettingsError):
            validate_project_settings({"ignore_globs": value})

    def test_a_non_mapping_document_is_rejected(self) -> None:
        with pytest.raises(ProjectSettingsError):
            validate_project_settings([])  # type: ignore[arg-type]


@pytest.mark.asyncio
class TestProjectTagUniqueness:
    async def test_the_same_tag_twice_on_one_project_is_rejected(self, conn) -> None:
        project_id = await make_project(conn, "tags")
        await conn.execute(INSERT_TAG, {"id": uuid.uuid4(), "project_id": project_id, "tag": "iac"})
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(INSERT_TAG, {"id": uuid.uuid4(), "project_id": project_id, "tag": "iac"})

    async def test_the_same_tag_on_two_projects_is_allowed(self, conn) -> None:
        """Tags are per project, not a global vocabulary."""
        first = await make_project(conn, "tags-p1")
        second = await make_project(conn, "tags-p2")
        for project_id in (first, second):
            await conn.execute(INSERT_TAG, {"id": uuid.uuid4(), "project_id": project_id, "tag": "iac"})

    async def test_deleting_a_project_removes_its_tags(self, conn) -> None:
        project_id = await make_project(conn, "tags-cascade")
        await conn.execute(INSERT_TAG, {"id": uuid.uuid4(), "project_id": project_id, "tag": "doomed"})
        await conn.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
        remaining = await scalar(conn, "SELECT count(*) FROM project_tags WHERE project_id = :p", p=project_id)
        assert remaining == 0


@pytest.mark.asyncio
class TestTheSchemaLevelNarrowing:
    async def test_a_json_array_in_settings_is_rejected(self, conn) -> None:
        """`settings` is a document, not a list. The one thing a check constraint can
        say about a JSONB column without becoming a migration per key."""
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text("INSERT INTO projects (id, name, path, settings) VALUES (:id, :name, '/tmp/x', '[]'::jsonb)"),
                    {"id": uuid.uuid4(), "name": f"array-settings-{uuid.uuid4().hex[:8]}"},
                )

    async def test_a_json_scalar_in_settings_is_rejected(self, conn) -> None:
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text("INSERT INTO projects (id, name, path, settings) VALUES (:id, :name, '/tmp/x', '42'::jsonb)"),
                    {"id": uuid.uuid4(), "name": f"scalar-settings-{uuid.uuid4().hex[:8]}"},
                )

    async def test_a_json_object_in_settings_is_accepted(self, conn) -> None:
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, path, settings) "
                "VALUES (:id, :name, '/tmp/x', '{\"favourite\": true}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "name": f"object-settings-{uuid.uuid4().hex[:8]}"},
        )
