"""Per-project learning history. Phase 2 §2.13.

WHAT THESE TESTS ARE FOR. A preference store shapes every future prompt, so the dangerous failure is not a
crash -- it is the system quietly believing something the user disagrees with and cannot change. The
assertions that matter most are therefore the CORRECTABILITY ones:

  * a stated preference is never overwritten by reflection
  * a deactivated preference is never reactivated by reflection
  * editing a reflected preference promotes it to stated, so the next pass cannot undo the correction
  * a preference that did not fit the budget is recorded as excluded rather than silently dropped

Each of those is a property the user relies on, and each fails silently if it regresses -- the store keeps
working, it just stops respecting them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.core.db import get_session
from src.core.errors import install_problem_handlers
from src.learning.memory import LearningMemory
from src.learning.reflector import (
    MIN_EVIDENCE,
    SKILL_FILE_CHAR_BUDGET,
    build_prompt,
    compile_skill_file,
    reflect,
    scope_for_path,
    statement_digest,
)
from src.learning.routes import router


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened


@pytest_asyncio.fixture
async def project_id(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": pid, "name": f"learn-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        await session.execute(text("DELETE FROM learning_feedback WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM learning_preferences WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM learning_sessions WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM learning_skill_files WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await session.commit()


class _Completion:
    def __init__(self, *, ok: bool = True, content: str | None = None) -> None:
        self.ok = ok
        self.content = content
        self.failure_reasons = ("unreachable",)
        self.served_from = "provider"
        self.endpoint_id = "ollama-primary"


class _Model:
    def __init__(self, completion: _Completion) -> None:
        self._completion = completion
        self.prompts: list[str] = []
        self.remembered: list[str] = []

    async def complete(self, *, prompt, on_token=None, may_serve_from_cache=True, store_in_cache=True):
        self.prompts.append(str(prompt))
        return self._completion

    async def remember(self, *, prompt, content: str) -> None:
        self.remembered.append(content)


def _app(session: AsyncSession, *, model: object | None = None) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router)
    app.state.artifact_model = model
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=uuid.uuid4(), subject="s", email="o@e.test", role=UserRole.ADMIN
    )
    return app


async def _feedback(
    session: AsyncSession, project_id: uuid.UUID, *, verdict: str = "rejected", path: str = "Dockerfile"
) -> None:
    await session.execute(
        text(
            "INSERT INTO learning_feedback (id, project_id, artifact_path, verdict, comment, final_content) "
            "VALUES (:id, :p, :path, :verdict, '', :final)"
        ),
        {
            "id": uuid.uuid4(),
            "p": project_id,
            "path": path,
            "verdict": verdict,
            "final": "FROM python:3.13-slim\n" if verdict == "edited" else "",
        },
    )


async def _preference(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    statement: str,
    scope: str = "dockerfile",
    source: str = "reflected",
    active: bool = True,
    evidence: int = 1,
) -> uuid.UUID:
    pref_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO learning_preferences "
            "(id, project_id, scope, statement, statement_digest, source, evidence_count, active) "
            "VALUES (:id, :p, :scope, :statement, :digest, :source, :evidence, :active)"
        ),
        {
            "id": pref_id,
            "p": project_id,
            "scope": scope,
            "statement": statement,
            "digest": statement_digest(statement),
            "source": source,
            "evidence": evidence,
            "active": active,
        },
    )
    return pref_id


class TestTheDatabaseRefusesAnUnusablePreference:
    async def test_an_empty_statement_is_refused(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """A preference with no text is one a user cannot read or disagree with."""
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO learning_preferences "
                    "(id, project_id, scope, statement, statement_digest, source) "
                    "VALUES (:id, :p, 'dockerfile', '', 'x', 'reflected')"
                ),
                {"id": uuid.uuid4(), "p": project_id},
            )
        await session.rollback()

    async def test_an_edited_verdict_without_the_result_is_refused(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Recording that something changed while discarding what it became."""
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO learning_feedback (id, project_id, artifact_path, verdict) "
                    "VALUES (:id, :p, 'Dockerfile', 'edited')"
                ),
                {"id": uuid.uuid4(), "p": project_id},
            )
        await session.rollback()

    async def test_the_same_statement_cannot_be_stored_twice_in_one_scope(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Without this the skill file fills with the same sentence and the budget buys repetition."""
        await _preference(session, project_id, statement="Pin every base image.")
        await session.commit()
        with pytest.raises(IntegrityError):
            await _preference(session, project_id, statement="Pin every base image.")
            await session.commit()
        await session.rollback()


class TestReflectionRefusesToInferFromNoise:
    async def test_below_the_evidence_floor_no_model_is_asked(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        await _feedback(session, project_id)
        await session.commit()
        model = _Model(_Completion(content="- [dockerfile] Pin every base image.\n"))
        result = await reflect(session, project_id=project_id, model=model)
        assert result.state == "insufficient_evidence"
        assert result.preferences_written == 0
        # The model was not consulted: an inference from one event would be a preference nobody stated.
        assert model.prompts == []
        assert f"floor of {MIN_EVIDENCE}" in result.explanation

    async def test_with_no_model_feedback_is_still_kept_and_stating_still_works(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        for _ in range(MIN_EVIDENCE):
            await _feedback(session, project_id)
        await session.commit()
        result = await reflect(session, project_id=project_id, model=None)
        assert result.state == "unavailable"
        assert "a stated preference needs no model" in result.explanation

    async def test_a_model_finding_no_pattern_is_a_real_answer(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        for _ in range(MIN_EVIDENCE):
            await _feedback(session, project_id)
        await session.commit()
        result = await reflect(session, project_id=project_id, model=_Model(_Completion(content="### NONE")))
        assert result.state == "nothing_inferred"
        assert result.preferences_written == 0
        assert "real answer rather than a failure" in result.explanation

    async def test_an_unrecognised_scope_is_dropped_not_coerced(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """A misfiled preference is injected into prompts it has nothing to do with."""
        for _ in range(MIN_EVIDENCE):
            await _feedback(session, project_id)
        await session.commit()
        result = await reflect(
            session,
            project_id=project_id,
            model=_Model(
                _Completion(
                    content=(
                        "- [nonsense] Something about nothing.\n"
                        "- [dockerfile] Pin every base image to an exact version.\n"
                    )
                )
            ),
        )
        await session.commit()
        assert result.preferences_written == 1
        scopes = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT scope FROM learning_preferences WHERE project_id = :p"), {"p": project_id}
                )
            ).all()
        ]
        assert scopes == ["dockerfile"]

    async def test_reflection_is_idempotent_on_statement_text(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        for _ in range(MIN_EVIDENCE):
            await _feedback(session, project_id)
        await session.commit()
        content = "- [dockerfile] Pin every base image to an exact version.\n"
        first = await reflect(session, project_id=project_id, model=_Model(_Completion(content=content)))
        await session.commit()
        second = await reflect(session, project_id=project_id, model=_Model(_Completion(content=content)))
        await session.commit()

        assert first.preferences_written == 1
        assert second.preferences_written == 0
        assert second.preferences_strengthened == 1
        count, evidence = (
            await session.execute(
                text("SELECT count(*), max(evidence_count) FROM learning_preferences WHERE project_id = :p"),
                {"p": project_id},
            )
        ).one()
        assert count == 1
        assert evidence == 2


class TestTheUsersDecisionsOutrankReflection:
    """The properties that make this store correctable rather than merely adaptive."""

    async def test_reflection_does_not_rewrite_a_stated_preference(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        statement = "Pin every base image to an exact version."
        await _preference(session, project_id, statement=statement, source="stated")
        for _ in range(MIN_EVIDENCE):
            await _feedback(session, project_id)
        await session.commit()

        result = await reflect(
            session,
            project_id=project_id,
            model=_Model(_Completion(content=f"- [dockerfile] {statement}\n")),
        )
        await session.commit()

        row = (
            (
                await session.execute(
                    text("SELECT source, statement, evidence_count FROM learning_preferences WHERE project_id = :p"),
                    {"p": project_id},
                )
            )
            .mappings()
            .one()
        )
        # Still stated, still the user's text, but the evidence count grew.
        assert row["source"] == "stated"
        assert row["statement"] == statement
        assert row["evidence_count"] == 2
        assert result.skipped_stated == 1
        assert "written by a person" in result.explanation

    async def test_reflection_does_not_reactivate_a_deactivated_preference(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Otherwise the user's checkbox un-checks itself on the next pass."""
        statement = "Always use port 8080."
        await _preference(session, project_id, statement=statement, active=False)
        for _ in range(MIN_EVIDENCE):
            await _feedback(session, project_id)
        await session.commit()

        await reflect(
            session,
            project_id=project_id,
            model=_Model(_Completion(content=f"- [dockerfile] {statement}\n")),
        )
        await session.commit()

        active = (
            await session.execute(
                text("SELECT active FROM learning_preferences WHERE project_id = :p"), {"p": project_id}
            )
        ).scalar_one()
        assert active is False

    async def test_editing_a_reflected_preference_promotes_it_to_stated(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Leaving it reflected would let the next pass treat the correction as its own and undo it."""
        pref_id = await _preference(session, project_id, statement="Use alpine.")
        await session.commit()

        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.patch(
                f"/learning/preferences/{pref_id}",
                json={"statement": "Use a slim Debian base, not alpine."},
            )
        assert response.status_code == 200
        assert "reflection cannot overwrite it" in response.json()["explanation"]

        row = (
            (
                await session.execute(
                    text("SELECT source, statement FROM learning_preferences WHERE id = :id"), {"id": pref_id}
                )
            )
            .mappings()
            .one()
        )
        assert row["source"] == "stated"
        assert "slim Debian" in row["statement"]

    async def test_deactivating_keeps_the_record_that_it_was_inferred(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Offering only deletion would force a user to destroy evidence to stop acting on it."""
        pref_id = await _preference(session, project_id, statement="Use alpine.")
        await session.commit()

        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            await client.patch(f"/learning/preferences/{pref_id}", json={"active": False})
            listing = await client.get(f"/projects/{project_id}/learning/preferences")

        body = listing.json()
        # STILL LISTED, so the user can see it is there and off.
        assert len(body["preferences"]) == 1
        assert body["preferences"][0]["active"] is False
        assert "0 active preference(s) of 1 recorded" in body["explanation"]

    async def test_deleting_a_preference_keeps_the_feedback_behind_it(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        pref_id = await _preference(session, project_id, statement="Use alpine.")
        await _feedback(session, project_id)
        await session.commit()

        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.delete(f"/learning/preferences/{pref_id}")
        assert response.status_code == 200
        assert "should not remove the observations behind it" in response.json()["explanation"]

        remaining = (
            await session.execute(
                text("SELECT count(*) FROM learning_feedback WHERE project_id = :p"), {"p": project_id}
            )
        ).scalar_one()
        assert remaining == 1

    async def test_restating_a_deactivated_preference_switches_it_back_on(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """The one path that may reactivate: the user asking for it unambiguously."""
        statement = "Pin every base image."
        await _preference(session, project_id, statement=statement, active=False)
        await session.commit()

        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_id}/learning/preferences",
                json={"scope": "dockerfile", "statement": statement},
            )
        assert response.status_code == 200
        assert "switched back on" in response.json()["explanation"]

        row = (
            (
                await session.execute(
                    text("SELECT source, active FROM learning_preferences WHERE project_id = :p"),
                    {"p": project_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["source"] == "stated"
        assert row["active"] is True


class TestTheSkillFileRecordsWhatItLeftOut:
    async def test_stated_preferences_come_first_regardless_of_evidence(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        await _preference(session, project_id, statement="Reflected with lots of evidence.", evidence=99)
        await _preference(session, project_id, statement="Stated with none.", source="stated", evidence=0)
        await session.commit()

        skill_file = await compile_skill_file(session, project_id=project_id)
        lines = [line for line in skill_file.content.splitlines() if line.startswith("- ")]
        assert "Stated with none." in lines[0]

    async def test_a_preference_that_does_not_fit_is_recorded_as_excluded(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """From the output alone, a dropped preference is indistinguishable from one that never existed."""
        long_statement = "x" * 900
        for index in range(4):
            await _preference(session, project_id, statement=f"{long_statement}{index}", evidence=10 - index)
        await session.commit()

        skill_file = await compile_skill_file(session, project_id=project_id)
        assert len(skill_file.content) <= SKILL_FILE_CHAR_BUDGET
        assert skill_file.excluded, "nothing was excluded, so the budget was not exercised"
        assert "did not fit" in skill_file.explanation

    async def test_an_inactive_preference_is_never_injected(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        await _preference(session, project_id, statement="Off.", active=False)
        await session.commit()
        skill_file = await compile_skill_file(session, project_id=project_id)
        assert skill_file.content == ""
        assert "no active preferences" in skill_file.explanation.lower()

    async def test_no_preferences_leaves_the_prompt_unchanged(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        skill_file = await compile_skill_file(session, project_id=project_id)
        assert skill_file.content == ""
        assert "honest state" in skill_file.explanation

    async def test_the_skill_file_route_names_what_was_excluded(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        long_statement = "y" * 900
        for index in range(4):
            await _preference(session, project_id, statement=f"{long_statement}{index}", evidence=10 - index)
        await session.commit()

        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get(f"/projects/{project_id}/learning/skill-file")
        body = response.json()
        assert body["excluded_preferences"], "the route did not report the exclusions"
        assert body["excluded_preferences"][0]["statement"].startswith("y")


class TestTheMemoryPortIsWhatGenerationSees:
    async def test_it_carries_the_exclusions_through(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """A caller receiving only the text could not record the difference, so the record would be wrong."""
        long_statement = "z" * 900
        for index in range(4):
            await _preference(session, project_id, statement=f"{long_statement}{index}", evidence=10 - index)
        await session.commit()

        memory = await LearningMemory().memory_for_prompt(session, project_id=project_id)
        assert memory.included
        assert memory.excluded
        assert memory.is_empty is False

    async def test_recording_an_injection_stores_exactly_what_was_sent(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        await _preference(session, project_id, statement="Pin every base image.")
        await session.commit()

        port = LearningMemory()
        memory = await port.memory_for_prompt(session, project_id=project_id)
        await port.record_injection(session, project_id=project_id, memory=memory)
        await session.commit()

        row = (
            (
                await session.execute(
                    text("SELECT content, included_preference_ids FROM learning_skill_files WHERE project_id = :p"),
                    {"p": project_id},
                )
            )
            .mappings()
            .one()
        )
        assert "Pin every base image." in row["content"]
        assert len(row["included_preference_ids"]) == 1

    async def test_an_empty_memory_is_reported_as_empty(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        memory = await LearningMemory().memory_for_prompt(session, project_id=project_id)
        assert memory.is_empty is True


class TestShortTermMemoryIsBoundedAndNeverPromoted:
    async def test_turns_are_trimmed_at_the_write_site(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """Bounded at write rather than by a sweep, so the bound holds at every moment."""
        from src.learning.reflector import MAX_SESSION_TURNS

        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for index in range(MAX_SESSION_TURNS + 5):
                await client.post(
                    f"/projects/{project_id}/learning/turns",
                    json={"session_key": "s1", "role": "user", "content": f"turn {index}"},
                )
        kept = (
            await session.execute(text("SELECT count(*) FROM learning_sessions WHERE session_key = 's1'"))
        ).scalar_one()
        assert kept <= MAX_SESSION_TURNS

    async def test_reflection_never_reads_conversation_turns(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Thinking aloud is not stating a preference."""
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for _ in range(5):
                await client.post(
                    f"/projects/{project_id}/learning/turns",
                    json={
                        "session_key": "s2",
                        "role": "user",
                        "content": "always use alpine for everything",
                    },
                )
        # Turns exist; feedback does not. Reflection must refuse for want of evidence.
        result = await reflect(
            session, project_id=project_id, model=_Model(_Completion(content="- [dockerfile] Use alpine.\n"))
        )
        assert result.state == "insufficient_evidence"
        assert result.feedback_considered == 0


class TestTheReflectionPromptPresentsEditsAsTheStrongestSignal:
    def test_an_edit_carries_what_was_kept(self) -> None:
        prompt = build_prompt(
            [
                {
                    "verdict": "edited",
                    "artifact_path": "Dockerfile",
                    "comment": "",
                    "final_content": "FROM python:3.13-slim\n",
                }
            ]
        )
        assert "EDITED Dockerfile" in prompt
        assert "what they kept instead" in prompt
        assert "python:3.13-slim" in prompt

    def test_the_model_is_told_not_to_restate_best_practice(self) -> None:
        """It is already in the generator's own requirements; repeating it spends the budget on nothing."""
        prompt = build_prompt([{"verdict": "accepted", "artifact_path": "x", "comment": ""}])
        assert "Do not state general best practice" in prompt
        assert "### NONE" in prompt


class TestScopeInferenceDefaultsRatherThanGuesses:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("Dockerfile", "dockerfile"),
            ("k8s/deployment.yaml", "kubernetes"),
            (".github/workflows/ci.yml", "ci"),
            ("terraform/main.tf", "iac"),
            ("README.md", "general"),
            ("some/unknown/thing.txt", "general"),
        ],
    )
    def test_scope_for_path(self, path: str, expected: str) -> None:
        assert scope_for_path(path) == expected


class TestFeedbackValidation:
    async def test_an_edited_verdict_without_content_is_refused_with_a_reason(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_id}/learning/feedback",
                json={"artifact_path": "Dockerfile", "verdict": "edited"},
            )
        assert response.status_code == 422
        assert "the only part a preference can be derived from" in response.text

    async def test_recording_feedback_does_not_infer_anything(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_id}/learning/feedback",
                json={"artifact_path": "k8s/deployment.yaml", "verdict": "rejected"},
            )
        body = response.json()
        assert body["inferred_scope"] == "kubernetes"
        assert "Nothing has been inferred from it yet" in body["explanation"]

    async def test_an_unknown_scope_is_refused_when_stating_a_preference(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{project_id}/learning/preferences",
                json={"scope": "invented", "statement": "Something."},
            )
        assert response.status_code == 422
        assert "misfiled preference" in response.text


class TestTheHistoryViewShowsBothHalves:
    async def test_it_distinguishes_never_told_from_told_and_ignored(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get(f"/projects/{project_id}/learning/history")
        body = response.json()
        assert body["feedback"] == []
        assert "different from having been told and ignoring it" in body["explanation"]
