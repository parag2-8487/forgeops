"""Guard-railed self-healing. Phase 2 §2.12.

THIS IS THE MOST SAFETY-CRITICAL TEST FILE IN THE PROJECT, because self-healing is the only feature that acts
on production without a human. The four properties the module claims are each tested as a property, not as a
happy path:

  1. The safe set is closed, and auto-execution is membership in it -- proven by asserting that EVERY risky
     remedy is refused by `/heal`, and that an unclassified name is refused too.
  2. An auto action cannot widen its scope -- proven by showing there is no request field for a target, and
     that the derived arguments always name the incident's own container.
  3. A healing loop cannot amplify -- proven against a real database for all three bounds, including that a
     failed remedy is disqualified permanently.
  4. Every action goes through the chokepoint -- proven by asserting the transit was called, and that a
     governance refusal marks the action failed rather than retrying.

The database guard rail is tested directly with raw SQL, because its whole purpose is to hold when a future
code path gets the decision wrong -- so a test that only went through the code path would not exercise it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
from src.incidents.healing import (
    COOLDOWN_SECONDS,
    MAX_ATTEMPTS_PER_INCIDENT,
    REMEDIES,
    RISKY_REMEDIES,
    SAFE_REMEDIES,
    HealingRefusedError,
    check_guard_rails,
    is_auto_executable,
    plan_from_incident,
    record_action,
)
from src.incidents.healing_routes import router as healing_router
from src.incidents.ingestion import from_container_exit, ingest
from src.incidents.postmortem import build_prompt, generate


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
        {"id": pid, "name": f"heal-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        # Incidents first: healing_actions cascade from them, and they hold the change_set references.
        await session.execute(text("DELETE FROM incidents WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM change_sets WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await session.commit()


class _Transit:
    """Records what the chokepoint was asked to do. Not a stand-in for governance: the tests using it are
    about whether the route CALLS governance with the right operation, which is unobservable otherwise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail = fail
        self.change_set_id = uuid.uuid4()

    async def transit_host_action(self, session, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("policy denied this action")
        # A REAL `change_sets` ROW, because `healing_actions.change_set_id` is a real foreign key and the
        # real chokepoint would have created one. Returning a bare uuid made the insert fail -- which is
        # the constraint doing its job: an action cannot point at an approval that does not exist.
        await session.execute(
            text(
                "INSERT INTO change_sets (id, project_id, status, origin, blast_radius_score, "
                "blast_radius_verdict, policy_bundle_digest) "
                "VALUES (:id, :p, 'approved', 'manual', 1, 'allow', 'sha256:test')"
            ),
            {"id": self.change_set_id, "p": kwargs["project_id"]},
        )
        return type("Submission", (), {"change_set_id": self.change_set_id})()


def _app(session: AsyncSession, chokepoint: object | None) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(healing_router)
    app.state.governance_chokepoint = chokepoint
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=uuid.uuid4(), subject="s", email="o@e.test", role=UserRole.ADMIN
    )
    return app


async def _container_incident(session: AsyncSession, project_id: uuid.UUID, container: str = "api"):
    result = await ingest(
        session, from_container_exit(project_id=project_id, container=container, exit_code=137, image="a")
    )
    await session.commit()
    return result.incident_id


# --- property 1: the safe set is closed -----------------------------------------------------------------


class TestTheSafeSetIsClosedAndAutoIsMembershipInIt:
    def test_auto_execution_is_exactly_membership_with_no_second_condition(self) -> None:
        for remedy in SAFE_REMEDIES:
            assert is_auto_executable(remedy) is True
        for remedy in RISKY_REMEDIES:
            assert is_auto_executable(remedy) is False

    def test_an_unclassified_remedy_is_risky_by_default(self) -> None:
        """The fail-safe direction: adding a remedy without classifying it gets a human, not an execution."""
        assert is_auto_executable("delete_everything") is False
        assert is_auto_executable("") is False

    def test_the_two_tiers_do_not_overlap(self) -> None:
        assert SAFE_REMEDIES & RISKY_REMEDIES == frozenset()

    def test_no_remedy_runs_a_command_line(self) -> None:
        """A self-healing system with a general-purpose action is a self-harming one with good intentions."""
        for remedy in REMEDIES:
            assert "exec" not in remedy
            assert "run" not in remedy
            assert "shell" not in remedy
            assert "command" not in remedy

    def test_nothing_destructive_is_in_the_safe_set(self) -> None:
        for remedy in SAFE_REMEDIES:
            for verb in ("delete", "remove", "destroy", "prune", "scale", "rollback"):
                assert verb not in remedy, f"{remedy} is in the safe set and contains {verb!r}"

    def test_the_migration_vocabulary_matches_the_python_one(self) -> None:
        """A guard rail that only exists in the language with the bug is not a guard rail."""
        from pathlib import Path

        migration = (Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0034_self_healing.py").read_text(
            encoding="utf-8"
        )
        for remedy in SAFE_REMEDIES:
            assert f'"{remedy}"' in migration
        for remedy in RISKY_REMEDIES:
            assert f'"{remedy}"' in migration
        # And the safe-set CHECK must name every safe remedy and no risky one.
        safe_line = next(line for line in migration.splitlines() if line.startswith("_SAFE = "))
        for remedy in RISKY_REMEDIES:
            assert remedy not in safe_line

    @pytest.mark.asyncio
    async def test_every_risky_remedy_is_refused_by_the_heal_route(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Not escalated, not queued, not executed with a warning. Refused."""
        incident_id = await _container_incident(session, project_id)
        transit = _Transit()
        app = _app(session, transit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            for remedy in sorted(RISKY_REMEDIES):
                response = await client.post(
                    f"/incidents/{incident_id}/heal",
                    json={"environment": "prod", "remedy": remedy},
                )
                assert response.status_code == 403, (remedy, response.text)
                assert "not in the safe set" in response.text
        # AND NOTHING REACHED GOVERNANCE, which is the stronger claim: no command was minted at all.
        assert transit.calls == []

    @pytest.mark.asyncio
    async def test_the_heal_request_has_no_field_for_an_auto_flag(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """A single route with `auto: bool` would put the most dangerous decision in the request body."""
        incident_id = await _container_incident(session, project_id)
        app = _app(session, _Transit())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/incidents/{incident_id}/heal",
                json={"environment": "prod", "auto": True},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_the_database_refuses_an_auto_row_for_a_risky_remedy(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Tested with raw SQL because its purpose is to hold when a FUTURE code path gets it wrong."""
        incident_id = await _container_incident(session, project_id)
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO healing_actions (id, incident_id, remedy, operation, arguments, auto, state) "
                    "VALUES (:id, :inc, 'rollback_deployment', 'x', '{}'::jsonb, true, 'executing')"
                ),
                {"id": uuid.uuid4(), "inc": incident_id},
            )
        await session.rollback()

    @pytest.mark.asyncio
    async def test_the_database_refuses_a_risky_execution_with_no_change_set(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """An approval-required action recorded as having run must point at what approved it."""
        incident_id = await _container_incident(session, project_id)
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO healing_actions (id, incident_id, remedy, operation, arguments, auto, state) "
                    "VALUES (:id, :inc, 'scale_workload', 'x', '{}'::jsonb, false, 'succeeded')"
                ),
                {"id": uuid.uuid4(), "inc": incident_id},
            )
        await session.rollback()


# --- property 2: an auto action cannot widen its own scope -----------------------------------------------


class TestAnAutoActionCannotWidenItsScope:
    def test_the_target_is_derived_from_the_incident_not_supplied(self) -> None:
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        assert plan.arguments == {"action": "restart", "container": "api"}
        assert plan.auto is True

    def test_an_incident_naming_no_target_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(HealingRefusedError, match="nothing to restart"):
            plan_from_incident(source="container_exit", detail={"exit_code": 137})

    def test_an_override_can_only_narrow_never_widen(self) -> None:
        """Naming a risky remedy produces auto=False; it never produces a risky auto-execution."""
        plan = plan_from_incident(
            source="container_exit",
            detail={"container": "api", "exit_code": 1},
            remedy="rollback_deployment",
        )
        assert plan.auto is False
        # And the target is STILL derived -- the override changed the verb, not what it acts on.
        assert plan.arguments["container"] == "api"

    def test_an_unknown_remedy_override_is_refused(self) -> None:
        with pytest.raises(HealingRefusedError, match="not a known remedy"):
            plan_from_incident(source="container_exit", detail={"container": "api"}, remedy="restart_everything")

    def test_there_is_no_default_remedy_for_an_unmapped_source(self) -> None:
        """A fallback remedy is one that runs against incidents nobody considered."""
        with pytest.raises(HealingRefusedError, match="no remedy is defined"):
            plan_from_incident(source="log_pattern", detail={"service": "api"})
        with pytest.raises(HealingRefusedError, match="no remedy is defined"):
            plan_from_incident(source="build_failure", detail={})

    def test_only_a_pod_is_restartable_not_any_kubernetes_kind(self) -> None:
        """Deleting a Deployment is not a restart, and one remedy meaning different things per kind is how
        an automated action does something nobody intended."""
        with pytest.raises(HealingRefusedError, match="not restartable"):
            plan_from_incident(
                source="kubernetes_event",
                detail={"namespace": "default", "name": "api", "kind": "Deployment"},
            )
        plan = plan_from_incident(
            source="kubernetes_event",
            detail={"namespace": "default", "name": "api", "kind": "Pod", "reason": "CrashLoopBackOff"},
        )
        assert plan.arguments == {"action": "restart", "namespace": "default", "pod": "api"}

    @pytest.mark.asyncio
    async def test_the_executed_arguments_name_the_incidents_own_container(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """End to end: what reached governance names the container the incident was about."""
        incident_id = await _container_incident(session, project_id, container="checkout-api")
        transit = _Transit()
        app = _app(session, transit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/incidents/{incident_id}/heal", json={"environment": "prod"})
        assert response.status_code == 200, response.text
        assert len(transit.calls) == 1
        assert transit.calls[0]["args"] == {"action": "restart", "container": "checkout-api"}
        assert transit.calls[0]["operation"] == "docker.container_action"


# --- property 3: a healing loop cannot amplify -----------------------------------------------------------


class TestAHealingLoopCannotAmplify:
    @pytest.mark.asyncio
    async def test_a_failed_remedy_is_disqualified_permanently(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """The bound that matters most: a restart that failed is evidence the restart is not the answer."""
        incident_id = await _container_incident(session, project_id)
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        await record_action(session, incident_id=incident_id, plan=plan, state="failed")
        await session.commit()

        with pytest.raises(HealingRefusedError, match="already been tried"):
            await check_guard_rails(session, incident_id=incident_id, remedy=plan.remedy)

        # Not even after the cooldown -- the disqualification is permanent, not a delay.
        with pytest.raises(HealingRefusedError, match="already been tried"):
            await check_guard_rails(
                session,
                incident_id=incident_id,
                remedy=plan.remedy,
                now=datetime.now(UTC) + timedelta(days=7),
            )

    @pytest.mark.asyncio
    async def test_the_attempt_budget_is_capped(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        incident_id = await _container_incident(session, project_id)
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        for _ in range(MAX_ATTEMPTS_PER_INCIDENT):
            await record_action(session, incident_id=incident_id, plan=plan, state="succeeded")
        await session.commit()

        with pytest.raises(HealingRefusedError, match="which is the limit"):
            await check_guard_rails(
                session,
                incident_id=incident_id,
                remedy="restart_pod",  # a DIFFERENT remedy still hits the per-incident cap
                now=datetime.now(UTC) + timedelta(days=1),
            )

    @pytest.mark.asyncio
    async def test_the_cooldown_stops_a_crash_loop_driving_the_restart_rate(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        incident_id = await _container_incident(session, project_id)
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        await record_action(session, incident_id=incident_id, plan=plan, state="succeeded")
        await session.commit()

        with pytest.raises(HealingRefusedError, match="cooldown"):
            await check_guard_rails(session, incident_id=incident_id, remedy="restart_pod")

        # And it clears once the window passes.
        await check_guard_rails(
            session,
            incident_id=incident_id,
            remedy="restart_pod",
            now=datetime.now(UTC) + timedelta(seconds=COOLDOWN_SECONDS + 1),
        )

    @pytest.mark.asyncio
    async def test_a_refused_action_does_not_consume_the_budget(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """A guard rail declining must not itself exhaust the budget -- that would make one refusal cascade
        into permanent refusal for reasons nobody chose."""
        incident_id = await _container_incident(session, project_id)
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        for _ in range(5):
            await record_action(session, incident_id=incident_id, plan=plan, state="refused")
        await session.commit()
        # No raise: refusals are excluded from the count.
        await check_guard_rails(session, incident_id=incident_id, remedy=plan.remedy)

    @pytest.mark.asyncio
    async def test_a_refusal_is_recorded_as_a_row_not_a_silence(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """ "The system did nothing" and "the system decided not to" are different facts."""
        incident_id = await _container_incident(session, project_id)
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        await record_action(session, incident_id=incident_id, plan=plan, state="succeeded")
        await session.commit()

        transit = _Transit()
        app = _app(session, transit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/incidents/{incident_id}/heal", json={"environment": "prod"})
        assert response.status_code == 409
        assert "cooldown" in response.text

        states = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT state FROM healing_actions WHERE incident_id = :id ORDER BY created_at"),
                    {"id": incident_id},
                )
            ).all()
        ]
        assert states == ["succeeded", "refused"]
        assert transit.calls == []

    @pytest.mark.asyncio
    async def test_a_governance_refusal_marks_the_action_failed_rather_than_retrying(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """If policy refuses a restart, retrying every cooldown is an automated system arguing with policy."""
        incident_id = await _container_incident(session, project_id)
        transit = _Transit(fail=True)
        app = _app(session, transit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/incidents/{incident_id}/heal", json={"environment": "prod"})
        assert response.status_code == 409
        assert "will not be retried automatically" in response.text

        row = (
            (
                await session.execute(
                    text("SELECT state, note FROM healing_actions WHERE incident_id = :id"),
                    {"id": incident_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["state"] == "failed"
        assert "governance refused" in row["note"]

        # And that failure now disqualifies the remedy, so the loop is closed.
        with pytest.raises(HealingRefusedError, match="already been tried"):
            await check_guard_rails(
                session,
                incident_id=incident_id,
                remedy="restart_container",
                now=datetime.now(UTC) + timedelta(days=1),
            )


# --- property 4: governance is never bypassed -----------------------------------------------------------


class TestGovernanceIsNeverBypassed:
    @pytest.mark.asyncio
    async def test_an_auto_action_still_goes_through_the_chokepoint(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        incident_id = await _container_incident(session, project_id)
        transit = _Transit()
        app = _app(session, transit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/incidents/{incident_id}/heal", json={"environment": "prod"})
        assert response.status_code == 200
        assert len(transit.calls) == 1
        # The reason reaching the audit row names self-healing, so an audit reader can tell an automated
        # restart from an operator's.
        assert "self-healing" in str(transit.calls[0]["reason"])
        body = response.json()
        assert body["change_set_id"] == str(transit.change_set_id)
        assert "nobody was asked, not that nothing was checked" in body["explanation"]

    @pytest.mark.asyncio
    async def test_with_no_chokepoint_nothing_is_attempted(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """An automated action with no audit row is the one thing this feature must never do."""
        incident_id = await _container_incident(session, project_id)
        app = _app(session, None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/incidents/{incident_id}/heal", json={"environment": "prod"})
        assert response.status_code == 503
        assert "not offered as an alternative" in response.text

    @pytest.mark.asyncio
    async def test_propose_mints_nothing(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        incident_id = await _container_incident(session, project_id)
        transit = _Transit()
        app = _app(session, transit)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/incidents/{incident_id}/heal/propose",
                json={"environment": "prod", "remedy": "rollback_deployment"},
            )
        assert response.status_code == 200
        assert transit.calls == []
        body = response.json()
        assert body["auto"] is False
        assert "no command has been signed" in body["explanation"]

        state = (
            await session.execute(
                text("SELECT state FROM healing_actions WHERE incident_id = :id"), {"id": incident_id}
            )
        ).scalar_one()
        assert state == "proposed"


class TestTheActivityLogIsHonest:
    @pytest.mark.asyncio
    async def test_no_actions_is_distinguished_from_declined_actions(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        incident_id = await _container_incident(session, project_id)
        app = _app(session, _Transit())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get(f"/incidents/{incident_id}/healing")
        body = response.json()
        assert body["actions"] == []
        assert "not the same as one having been attempted and declined" in body["explanation"]

    @pytest.mark.asyncio
    async def test_the_log_reports_the_remaining_budget_and_disqualifications(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        incident_id = await _container_incident(session, project_id)
        plan = plan_from_incident(source="container_exit", detail={"container": "api", "exit_code": 137})
        await record_action(session, incident_id=incident_id, plan=plan, state="failed")
        await session.commit()

        app = _app(session, _Transit())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get(f"/incidents/{incident_id}/healing")
        body = response.json()
        assert body["attempts_used"] == 1
        assert body["attempts_remaining"] == MAX_ATTEMPTS_PER_INCIDENT - 1
        assert body["disqualified_remedies"] == ["restart_container"]
        assert "will not be retried" in body["explanation"]

    @pytest.mark.asyncio
    async def test_the_remedy_catalogue_is_published_so_the_split_is_inspectable(self, session: AsyncSession) -> None:
        app = _app(session, _Transit())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/healing/remedies")
        body = response.json()
        assert set(body["safe"]) == SAFE_REMEDIES
        assert set(body["risky"]) == RISKY_REMEDIES
        assert "Nothing in either tier runs a command line" in body["explanation"]


class TestThePostmortemRefusesToFabricate:
    class _Completion:
        def __init__(self, *, ok: bool = True, content: str | None = None) -> None:
            self.ok = ok
            self.content = content
            self.failure_reasons = ("unreachable",)
            self.served_from = "provider"
            self.endpoint_id = "ollama-primary"

    class _Model:
        def __init__(self, completion: object) -> None:
            self._completion = completion
            self.remembered: list[str] = []

        async def complete(self, *, prompt, on_token=None, may_serve_from_cache=True, store_in_cache=True):
            return self._completion

        async def remember(self, *, prompt, content: str) -> None:
            self.remembered.append(content)

    @pytest.mark.asyncio
    async def test_no_model_is_unavailable_not_an_empty_summary(self) -> None:
        result = await generate(
            model=None, title="t", source="container_exit", analysis_state="analysed", problem="p", actions=[]
        )
        assert result.state == "unavailable"
        assert result.summary == ""
        assert "what is missing is the narrative" in result.explanation

    @pytest.mark.asyncio
    async def test_an_unparseable_answer_does_not_become_the_summary(self) -> None:
        model = self._Model(self._Completion(content="Well, some things happened."))
        result = await generate(
            model=model, title="t", source="container_exit", analysis_state="analysed", problem="p", actions=[]
        )
        assert result.state == "insufficient"
        assert result.summary == ""
        assert "could not parse" in result.explanation
        assert model.remembered == []

    @pytest.mark.asyncio
    async def test_a_none_supported_recommendation_is_not_carried_through(self) -> None:
        """Carrying it would put a non-recommendation in a list an operator scans for things to do."""
        model = self._Model(
            self._Completion(
                content=(
                    "### SUMMARY\nThe container ran out of memory and was restarted.\n"
                    "### RECOMMENDATIONS\n- none supported by this record\n"
                )
            )
        )
        result = await generate(
            model=model, title="t", source="container_exit", analysis_state="analysed", problem="p", actions=[]
        )
        assert result.state == "generated"
        assert result.recommendations == []

    @pytest.mark.asyncio
    async def test_recommendations_are_parsed_as_a_list(self) -> None:
        model = self._Model(
            self._Completion(
                content=(
                    "### SUMMARY\nOOM, restarted, recovered.\n"
                    "### RECOMMENDATIONS\n- Raise the memory limit.\n- Add a memory alert.\n"
                )
            )
        )
        result = await generate(
            model=model, title="t", source="container_exit", analysis_state="analysed", problem="p", actions=[]
        )
        assert result.recommendations == ["Raise the memory limit.", "Add a memory alert."]

    def test_declined_actions_appear_in_the_prompt_as_declined(self) -> None:
        """An incident where every remedy was declined must not look like one nobody responded to."""
        prompt = build_prompt(
            title="t",
            source="container_exit",
            analysis_state="analysed",
            problem="p",
            actions=[{"remedy": "restart_container", "auto": True, "state": "refused", "note": "cooldown"}],
        )
        assert "[DECLINED] restart_container" in prompt
        assert "including ones that were declined" in prompt

    def test_with_no_cause_the_prompt_forbids_inferring_one(self) -> None:
        prompt = build_prompt(title="t", source="container_exit", analysis_state="inconclusive", problem="", actions=[])
        assert "Do not infer one" in prompt
