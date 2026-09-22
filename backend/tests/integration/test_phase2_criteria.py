"""Phase 2 completion criteria, driven end to end. Phase 2 completion evidence.

WHY THESE ARE TESTS RATHER THAN A ONE-OFF SCRIPT. Each criterion needs evidence from a real run, and a run
whose evidence lives only in a session transcript is evidence that expires: the next change can break the
criterion and nothing notices. Written as tests, the criteria are re-checked on every run.

Each test names the criterion it satisfies in its docstring, and each drives the REAL chain rather than
asserting on a unit -- `heal` through the chokepoint, `reflect` into an injected prompt -- because the
criteria are about the system working, not about a function returning.
"""

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.core.db import get_session
from src.core.errors import install_problem_handlers
from src.incidents.healing_routes import router as healing_router
from src.incidents.ingestion import from_container_exit, ingest


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened


@pytest_asyncio.fixture
async def project_id(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :n, :p)"),
        {"id": pid, "n": f"crit-{pid.hex[:8]}", "p": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        await session.execute(text("DELETE FROM incidents WHERE project_id = :p"), {"p": pid})
        await session.execute(text("DELETE FROM change_sets WHERE project_id = :p"), {"p": pid})
        await session.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        await session.commit()


class _Transit:
    def __init__(self):
        self.calls = []
        self.change_set_id = uuid.uuid4()

    async def transit_host_action(self, session, **kwargs):
        self.calls.append(kwargs)
        await session.execute(
            text(
                "INSERT INTO change_sets (id, project_id, status, origin, blast_radius_score, "
                "blast_radius_verdict, policy_bundle_digest) "
                "VALUES (:id, :p, 'approved', 'manual', 1, 'allow', 'sha256:t')"
            ),
            {"id": self.change_set_id, "p": kwargs["project_id"]},
        )
        return type("S", (), {"change_set_id": self.change_set_id})()


class _Model:
    async def complete(self, *, prompt, on_token=None, may_serve_from_cache=True, store_in_cache=True):
        return type(
            "C",
            (),
            {
                "ok": True,
                "content": (
                    "### SUMMARY\nThe api container exited with code 137, was restarted automatically, "
                    "and returned to service.\n"
                    "### RECOMMENDATIONS\n- Raise the container memory limit.\n"
                ),
                "failure_reasons": (),
                "served_from": "provider",
                "endpoint_id": "ollama-primary",
            },
        )()

    async def remember(self, *, prompt, content):
        pass


async def test_criterion_failed_container_is_auto_restarted_with_a_log(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """CRITERION: Failed container is auto-restarted (with log)."""
    result = await ingest(
        session, from_container_exit(project_id=project_id, container="api", exit_code=137, image="api:1")
    )
    await session.commit()

    transit = _Transit()
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(healing_router)
    app.state.governance_chokepoint = transit
    app.state.artifact_model = _Model()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=uuid.uuid4(), subject="s", email="o@e.test", role=UserRole.ADMIN
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        heal = await client.post(f"/incidents/{result.incident_id}/heal", json={"environment": "prod"})
        assert heal.status_code == 200, heal.text
        body = heal.json()
        print("CRITERION-RESTART auto=", body["auto"], "remedy=", body["remedy"])
        assert body["auto"] is True
        assert body["remedy"] == "restart_container"

        log = await client.get(f"/incidents/{result.incident_id}/healing")
        entries = log.json()["actions"]
        print("CRITERION-RESTART log rows=", len(entries), "state=", entries[0]["state"])
        assert entries[0]["state"] == "executing"
        assert entries[0]["auto"] is True
        assert entries[0]["arguments"] == {"action": "restart", "container": "api"}

        pm = await client.post(f"/incidents/{result.incident_id}/postmortem")
        summary = pm.json()
        print("CRITERION-POSTMORTEM state=", summary["state"])
        print("CRITERION-POSTMORTEM summary=", summary["summary"][:90])
        print("CRITERION-POSTMORTEM recs=", summary["recommendations"])
        assert summary["state"] == "generated"
        assert "restarted automatically" in summary["summary"]
        assert summary["recommendations"] == ["Raise the container memory limit."]

    assert transit.calls[0]["operation"] == "docker.container_action"
    print("CRITERION-GOVERNANCE operation=", transit.calls[0]["operation"])


async def test_criterion_ai_learns_from_feedback_and_the_preference_reaches_a_prompt(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    """CRITERION: AI learns from accepted/rejected suggestions (does not re-suggest rejected patterns).

    The chain that matters: reject something, reflect, and prove the resulting preference is INJECTED into
    what a future prompt would carry. Proving only that a row was written would prove the system remembered
    and not that it acts on the memory.
    """
    from src.learning.memory import LearningMemory
    from src.learning.reflector import reflect

    for path in ("Dockerfile", "Dockerfile"):
        await session.execute(
            text(
                "INSERT INTO learning_feedback (id, project_id, artifact_path, verdict, comment) "
                "VALUES (:id, :p, :path, 'rejected', :comment)"
            ),
            {
                "id": uuid.uuid4(),
                "p": project_id,
                "path": path,
                "comment": "we never use alpine here",
            },
        )
    await session.commit()

    class _Reflector:
        async def complete(self, *, prompt, on_token=None, may_serve_from_cache=True, store_in_cache=True):
            # The prompt must carry the rejections, or reflection is inferring from nothing.
            assert "REJECTED Dockerfile" in str(prompt), str(prompt)[:300]
            assert "we never use alpine here" in str(prompt)
            return type(
                "C",
                (),
                {
                    "ok": True,
                    "content": "- [dockerfile] Never use an alpine base image in this project.\n",
                    "failure_reasons": (),
                    "served_from": "provider",
                    "endpoint_id": "ollama-primary",
                },
            )()

        async def remember(self, *, prompt, content):
            pass

    result = await reflect(session, project_id=project_id, model=_Reflector())
    await session.commit()
    print("CRITERION-LEARN reflected state=", result.state, "written=", result.preferences_written)
    assert result.state == "reflected"
    assert result.preferences_written == 1

    # THE PART THAT MATTERS: the preference reaches what a prompt would carry.
    memory = await LearningMemory().memory_for_prompt(session, project_id=project_id)
    print("CRITERION-LEARN injected=", memory.content.strip().replace(chr(10), " | "))
    assert "Never use an alpine base image" in memory.content
    assert len(memory.included) == 1

    # And it is recorded as having been injected, so a run can be judged against it.
    await LearningMemory().record_injection(session, project_id=project_id, memory=memory)
    await session.commit()
    stored = (
        await session.execute(text("SELECT content FROM learning_skill_files WHERE project_id = :p"), {"p": project_id})
    ).scalar_one()
    print("CRITERION-LEARN recorded injection bytes=", len(stored))
    assert "alpine" in stored

    await session.execute(text("DELETE FROM learning_skill_files WHERE project_id = :p"), {"p": project_id})
    await session.execute(text("DELETE FROM learning_preferences WHERE project_id = :p"), {"p": project_id})
    await session.execute(text("DELETE FROM learning_feedback WHERE project_id = :p"), {"p": project_id})
    await session.commit()
