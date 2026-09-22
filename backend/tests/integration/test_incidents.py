"""Incidents and root-cause analysis. Phase 2 §2.11.

WHAT THESE TESTS ARE FOR. An RCA is a thing an operator ACTS on -- they restart the wrong service, roll back a
release that was fine, or spend an outage reading the wrong logs. So the assertions that matter most are the
ones proving the pipeline REFUSES to conclude: below the evidence floor, with no model, and when the model
answers in a shape that was not understood. A test suite that only checked the happy path would pass on a
pipeline that fabricated a cause whenever it could not find one.

The database constraints are exercised against a REAL Postgres rather than asserted from the migration text,
because a CHECK constraint that was written and never tested is a constraint that might be spelled wrong --
and the failure mode is silent acceptance of exactly the row it was meant to refuse.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.core.metrics_port import EvidenceRecord
from src.incidents import analysis as rca
from src.incidents.evidence import collect_log_evidence, evidence_floor
from src.incidents.ingestion import (
    IncidentSourceError,
    Observation,
    from_build_failure,
    from_circuit_breaker,
    from_container_exit,
    from_deployment_failure,
    from_kubernetes_event,
    ingest,
)


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A real session against the migrated database.

    Not the rolled-back `conn` fixture: `ingest` COMMITs, and the commit is part of what is under test --
    the ON CONFLICT arbitration only happens against committed rows.
    """
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened


@pytest_asyncio.fixture
async def project_id(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    """A real `projects` row, because `incidents.project_id` is a real foreign key."""
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": pid, "name": f"incident-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        # ON DELETE CASCADE removes the evidence, analyses and suggestions with the incidents.
        await session.execute(text("DELETE FROM incidents WHERE project_id = :id"), {"id": pid})
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await session.commit()


class _Completion:
    """A `ModelCompletion` shape. Not a stand-in for a model: these tests are about what the PIPELINE does
    with an answer, and driving a real 100-second CPU completion to assert on parsing would test the model."""

    def __init__(self, *, ok: bool = True, content: str | None = None, reasons: tuple[str, ...] = ()) -> None:
        self.ok = ok
        self.content = content
        self.failure_reasons = reasons
        self.served_from = "provider"
        self.endpoint_id = "ollama-primary"


class _Model:
    def __init__(self, completion: _Completion) -> None:
        self._completion = completion
        self.remembered: list[str] = []
        self.tier_name = "self_hosted"

    async def complete(self, *, prompt, on_token=None, may_serve_from_cache=True, store_in_cache=True):
        return self._completion

    async def remember(self, *, prompt, content: str) -> None:
        self.remembered.append(content)


def _readable(kind: str) -> EvidenceRecord:
    return EvidenceRecord(kind=kind, reachable=True, summary=f"{kind} answered with something.")


def _unreadable(kind: str) -> EvidenceRecord:
    return EvidenceRecord(kind=kind, reachable=False, summary=f"{kind} could not be read.")


class TestIngestionDeduplicatesByFingerprint:
    async def test_the_same_failure_increments_rather_than_inserting(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """A crash loop must be one incident saying 47 times, not 47 incidents burying everything else."""
        observation = from_container_exit(project_id=project_id, container="api", exit_code=137, image="api:1")
        first = await ingest(session, observation)
        second = await ingest(session, observation)
        third = await ingest(session, observation)
        await session.commit()

        assert first.created is True
        assert second.created is False
        assert third.occurrences == 3
        assert second.incident_id == first.incident_id

        count = (
            await session.execute(text("SELECT count(*) FROM incidents WHERE project_id = :p"), {"p": project_id})
        ).scalar_one()
        assert count == 1

    async def test_a_different_exit_code_is_a_different_incident(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        a = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=137, image="api:1")
        )
        b = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=1, image="api:1")
        )
        await session.commit()
        assert a.incident_id != b.incident_id

    async def test_severity_rises_but_never_falls(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """The worst thing this incident has ever been is what an operator needs to see."""
        warning = Observation(
            project_id=project_id,
            source="kubernetes_event",
            severity="warning",
            title="pod is unhappy",
            identity=("default", "Pod", "api"),
        )
        critical = Observation(
            project_id=project_id,
            source="kubernetes_event",
            severity="critical",
            title="pod is failing",
            identity=("default", "Pod", "api"),
        )
        await ingest(session, warning)
        await ingest(session, critical)
        await ingest(session, warning)  # a later warning must NOT downgrade it
        await session.commit()

        severity = (
            await session.execute(text("SELECT severity FROM incidents WHERE project_id = :p"), {"p": project_id})
        ).scalar_one()
        assert severity == "critical"

    async def test_a_resolved_incident_recurring_files_a_new_one(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Folding a regression back into a dismissed row would hide it inside something already closed."""
        observation = from_circuit_breaker(project_id=project_id, environment="prod", failures=3)
        first = await ingest(session, observation)
        await session.execute(
            text("UPDATE incidents SET resolved_at = now() WHERE id = :id"), {"id": first.incident_id}
        )
        await session.commit()

        second = await ingest(session, observation)
        await session.commit()
        assert second.incident_id != first.incident_id
        assert second.created is True


class TestAnObservationRefusesWhatCannotBeGrouped:
    def test_an_identity_free_observation_is_refused(self) -> None:
        with pytest.raises(IncidentSourceError, match="cannot be deduplicated"):
            Observation(
                project_id=uuid.uuid4(),
                source="log_pattern",
                severity="info",
                title="something",
                identity=(),
            )

    def test_an_unknown_source_is_refused_before_the_database_sees_it(self) -> None:
        with pytest.raises(IncidentSourceError, match="not an incident source"):
            Observation(
                project_id=uuid.uuid4(),
                source="invented",
                severity="info",
                title="something",
                identity=("x",),
            )

    def test_a_clean_container_exit_is_not_an_incident(self) -> None:
        """Admitting exit 0 would fill the list with every completed job and bury the failures."""
        with pytest.raises(IncidentSourceError, match="clean exit"):
            from_container_exit(project_id=uuid.uuid4(), container="job", exit_code=0, image="job:1")

    def test_a_deployment_failure_is_keyed_on_the_failure_not_the_attempt(self) -> None:
        """Keying on the deployment id would make a redeploy loop look like progress."""
        one = from_deployment_failure(
            project_id=uuid.UUID(int=1),
            deployment_id=uuid.uuid4(),
            environment="prod",
            operation="tofu apply",
            reason="quota exceeded",
        )
        two = from_deployment_failure(
            project_id=uuid.UUID(int=1),
            deployment_id=uuid.uuid4(),  # a different attempt
            environment="prod",
            operation="tofu apply",
            reason="quota exceeded",
        )
        assert one.fingerprint == two.fingerprint

    def test_a_build_failure_is_keyed_on_the_failing_checks(self) -> None:
        """Twenty runs failing one check are one incident saying the check cannot be satisfied."""
        one = from_build_failure(
            project_id=uuid.UUID(int=2),
            run_id=uuid.uuid4(),
            findings=("dockerfile_healthcheck_present: missing HEALTHCHECK",),
        )
        two = from_build_failure(
            project_id=uuid.UUID(int=2),
            run_id=uuid.uuid4(),
            findings=("dockerfile_healthcheck_present: still missing",),
        )
        assert one.fingerprint == two.fingerprint

    def test_two_projects_with_the_same_container_name_do_not_share_an_incident(self) -> None:
        one = from_container_exit(project_id=uuid.UUID(int=3), container="api", exit_code=1, image="a")
        two = from_container_exit(project_id=uuid.UUID(int=4), container="api", exit_code=1, image="a")
        assert one.fingerprint != two.fingerprint

    def test_a_kubernetes_crashloop_is_critical_and_a_plain_warning_is_not(self) -> None:
        crash = from_kubernetes_event(
            project_id=uuid.uuid4(),
            namespace="default",
            kind="Pod",
            name="api",
            reason="CrashLoopBackOff",
            message="back-off restarting",
        )
        unhealthy = from_kubernetes_event(
            project_id=uuid.uuid4(),
            namespace="default",
            kind="Pod",
            name="api",
            reason="Unhealthy",
            message="probe failed",
        )
        assert crash.severity == "critical"
        assert unhealthy.severity == "warning"


class TestTheDatabaseRefusesADishonestAnalysis:
    """Asserted against a real Postgres, because a CHECK constraint never exercised might be misspelled."""

    async def test_analysed_without_a_problem_is_refused(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        result = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=1, image="a")
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO incident_analyses (id, incident_id, state, problem, location) "
                    "VALUES (:id, :inc, 'analysed', '', '')"
                ),
                {"id": uuid.uuid4(), "inc": result.incident_id},
            )
        await session.rollback()

    async def test_a_cause_without_the_analysed_state_is_refused(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """A conclusion nothing stands behind."""
        result = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=1, image="a")
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO incident_analyses (id, incident_id, state, problem, location) "
                    "VALUES (:id, :inc, 'inconclusive', 'the disk is full', 'node-1')"
                ),
                {"id": uuid.uuid4(), "inc": result.incident_id},
            )
        await session.rollback()

    async def test_more_reachable_than_consulted_is_refused(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        result = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=1, image="a")
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO incident_analyses (id, incident_id, state, evidence_reachable, "
                    "evidence_consulted) VALUES (:id, :inc, 'inconclusive', 5, 2)"
                ),
                {"id": uuid.uuid4(), "inc": result.incident_id},
            )
        await session.rollback()


class TestTheAnalysisRefusesToConclude:
    """The tests this module exists for."""

    async def test_below_the_evidence_floor_no_model_is_even_asked(self) -> None:
        model = _Model(_Completion(content="### PROBLEM\nthe disk is full\n### LOCATION\nnode-1\n"))
        result = await rca.analyse(
            model=model,
            title="pod failed",
            source="kubernetes_event",
            detail={},
            findings=[_readable("metrics"), _unreadable("logs"), _unreadable("kubernetes")],
        )
        assert result.state == "insufficient_evidence"
        assert result.problem == ""
        # And the model was not consulted at all -- asking it would produce an answer somebody might read.
        assert model.remembered == []
        assert "not enough to identify a cause" in result.explanation
        # Sorted, so the sentence is stable across runs rather than depending on collection order.
        assert "kubernetes, logs unavailable" in result.explanation

    async def test_with_no_model_the_evidence_is_still_kept(self) -> None:
        result = await rca.analyse(
            model=None,
            title="pod failed",
            source="kubernetes_event",
            detail={},
            findings=[_readable("metrics"), _readable("logs")],
        )
        assert result.state == "unavailable"
        assert result.evidence_reachable == 2
        assert "collected but not interpreted" in result.explanation
        # Explicitly: the distinction between "no inference" and "no data".
        assert "not the data" in result.explanation

    async def test_an_unparseable_answer_is_inconclusive_not_a_problem_field(self) -> None:
        """Stuffing prose into `problem` would satisfy the database and be exactly the lie it refuses."""
        model = _Model(_Completion(content="I think maybe the disk is full, but I am not sure."))
        result = await rca.analyse(
            model=model,
            title="pod failed",
            source="kubernetes_event",
            detail={},
            findings=[_readable("metrics"), _readable("logs")],
        )
        assert result.state == "inconclusive"
        assert result.problem == ""
        assert "could not parse" in result.explanation
        # The raw reply is kept so a human can read what the model actually said.
        assert "disk is full" in result.explanation
        # And an unparseable answer is NOT cached.
        assert model.remembered == []

    async def test_a_model_saying_it_cannot_tell_is_a_real_answer(self) -> None:
        model = _Model(_Completion(content="### INSUFFICIENT"))
        result = await rca.analyse(
            model=model,
            title="pod failed",
            source="kubernetes_event",
            detail={},
            findings=[_readable("metrics"), _readable("logs")],
        )
        assert result.state == "inconclusive"
        assert "real answer rather than a failure" in result.explanation
        assert result.model == "ollama-primary"

    async def test_an_unreachable_model_does_not_become_a_conclusion(self) -> None:
        model = _Model(_Completion(ok=False, reasons=("every endpoint was skipped",)))
        result = await rca.analyse(
            model=model,
            title="pod failed",
            source="kubernetes_event",
            detail={},
            findings=[_readable("metrics"), _readable("logs")],
        )
        assert result.state == "unavailable"
        assert result.problem == ""
        assert "every endpoint was skipped" in result.explanation

    async def test_a_well_formed_answer_is_recorded_and_cached(self) -> None:
        model = _Model(
            _Completion(
                content=(
                    "### PROBLEM\nThe container exceeded its memory limit.\n"
                    "### LOCATION\ndeploy/api.yaml, resources.limits.memory\n"
                    "### FIX\nRaise the limit to 512Mi.\n"
                )
            )
        )
        result = await rca.analyse(
            model=model,
            title="container api exited 137",
            source="container_exit",
            detail={"exit_code": 137},
            findings=[_readable("metrics"), _readable("logs")],
        )
        assert result.state == "analysed"
        assert "memory limit" in result.problem
        assert "deploy/api.yaml" in result.location
        assert "512Mi" in result.fix
        assert result.evidence_reachable == 2
        assert len(model.remembered) == 1


class TestThePromptTellsTheModelWhatIsMissing:
    def test_unreachable_sources_are_named_in_the_prompt(self) -> None:
        """A model shown only what was readable reasons as though that were everything."""
        prompt = rca.build_prompt(
            title="pod failed",
            source="kubernetes_event",
            detail={},
            findings=[_readable("metrics"), _unreadable("logs")],
        )
        assert "EVIDENCE THAT COULD NOT BE READ" in prompt
        assert "logs could not be read" in prompt
        # And it must be told it may decline.
        assert "### INSUFFICIENT" in prompt
        assert "Do not guess" in prompt


class TestEvidenceDistinguishesAbsentFromEmpty:
    async def test_no_log_store_is_not_an_absence_of_errors(self) -> None:
        finding = await collect_log_evidence(None, service="api")
        assert finding.reachable is False
        assert "No log store is configured" in finding.summary

    async def test_a_store_that_answers_with_nothing_is_reachable(self) -> None:
        """The store answered and there are no errors -- real evidence, not a gap."""

        class _Empty:
            async def recent_errors(self, *, service: str, limit: int):
                return True, "", []

        finding = await collect_log_evidence(_Empty(), service="api")
        assert finding.reachable is True
        assert "did not log an error" in finding.summary

    async def test_a_raising_log_store_does_not_fail_the_analysis(self) -> None:
        """An RCA that crashes because the log store is down is useless exactly when it is needed."""

        class _Broken:
            async def recent_errors(self, *, service: str, limit: int):
                raise ConnectionError("refused")

        finding = await collect_log_evidence(_Broken(), service="api")
        assert finding.reachable is False
        assert "could not be read" in finding.summary

    def test_the_floor_names_which_sources_were_missing(self) -> None:
        enough, note = evidence_floor([_readable("metrics"), _unreadable("logs")])
        assert enough is False
        assert "logs unavailable" in note

    def test_two_readable_sources_clear_the_floor(self) -> None:
        enough, note = evidence_floor([_readable("metrics"), _readable("logs"), _unreadable("kubernetes")])
        assert enough is True
        assert "2 of 3" in note


class TestPersistenceKeepsEvidenceWhateverWasConcluded:
    async def test_evidence_is_written_even_when_nothing_was_concluded(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """The whole value of the evidence table: "no cause" still tells an operator what was looked at."""
        result = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=137, image="a")
        )
        findings = [_readable("metrics"), _unreadable("logs")]
        analysis = await rca.analyse(model=None, title="t", source="container_exit", detail={}, findings=findings)
        await rca.persist(session, incident_id=result.incident_id, findings=findings, analysis=analysis)
        await session.commit()

        rows = (
            await session.execute(
                text("SELECT kind, reachable FROM incident_evidence WHERE incident_id = :id ORDER BY kind"),
                {"id": result.incident_id},
            )
        ).all()
        assert [(row[0], row[1]) for row in rows] == [("logs", False), ("metrics", True)]

        state = (
            await session.execute(
                text("SELECT state FROM incident_analyses WHERE incident_id = :id"),
                {"id": result.incident_id},
            )
        ).scalar_one()
        assert state == "insufficient_evidence"


class TestTheProductionCallerActuallyFilesAnIncident:
    """The recurring defect this project keeps finding is an ingestion path with no production caller.

    `DeploymentService.complete` had none. `record_command_result`'s failure branch could not run.
    `evaluate_policy` took an argument nobody passed. So this drives the real `DeploymentService.fail`
    and reads the incident row back, rather than calling `ingest` directly and assuming the wiring.
    """

    async def test_a_real_deployment_failure_produces_an_incident_row(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        from src.deployments.service import DeploymentService
        from src.incidents.recorders import DeploymentIncidents

        environment_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO environments (id, project_id, name, kind, position) "
                "VALUES (:id, :p, 'prod', 'production', 1)"
            ),
            {"id": environment_id, "p": project_id},
        )
        await session.commit()

        service = DeploymentService(incidents=DeploymentIncidents())
        deployment = await service.create(
            session,
            project_id=project_id,
            environment_id=environment_id,
            tenant_id=None,
            manifests=["deploy/api.yaml"],
            cluster_context=None,
            namespace=None,
            requested_by=None,
        )
        await session.commit()

        # THE REAL FAILURE BRANCH -- reached only when the agent ran the command and it failed.
        await service.fail(
            session,
            deployment_id=deployment.id,
            reason="the apply was refused: quota exceeded",
        )
        await session.commit()

        row = (
            (
                await session.execute(
                    text("SELECT source, severity, title, origin_kind, origin_id FROM incidents WHERE project_id = :p"),
                    {"p": project_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["source"] == "deployment_failure"
        assert row["severity"] == "critical"
        assert "quota exceeded" in row["title"]
        assert row["origin_kind"] == "deployment"
        # The incident traces back to the deployment that produced it.
        assert row["origin_id"] == str(deployment.id)

    async def test_a_deployment_service_without_a_recorder_still_fails_cleanly(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Incident ingestion is optional; a deployment must not depend on it to fail."""
        from src.deployments.service import DeploymentService

        environment_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO environments (id, project_id, name, kind, position) "
                "VALUES (:id, :p, 'dev', 'development', 1)"
            ),
            {"id": environment_id, "p": project_id},
        )
        await session.commit()

        service = DeploymentService()  # no recorder
        deployment = await service.create(
            session,
            project_id=project_id,
            environment_id=environment_id,
            tenant_id=None,
            manifests=["deploy/api.yaml"],
            cluster_context=None,
            namespace=None,
            requested_by=None,
        )
        await session.commit()
        record = await service.fail(session, deployment_id=deployment.id, reason="refused")
        await session.commit()

        assert record.status == "failed"
        # `healthy` stays NULL: nothing checked the workloads, and False would assert that they were
        # checked and not ready.
        assert record.healthy is None
        count = (
            await session.execute(text("SELECT count(*) FROM incidents WHERE project_id = :p"), {"p": project_id})
        ).scalar_one()
        assert count == 0


class TestTheAnalyseRouteIsTheProductionCaller:
    """The pipeline needs a caller or it is the recurring defect again. This drives the route."""

    async def test_the_route_records_an_analysis_and_its_evidence(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from src.auth.dependencies import require_principal
        from src.auth.models import UserRole
        from src.auth.principal import Principal
        from src.core.db import get_session
        from src.core.errors import install_problem_handlers
        from src.incidents.routes import router

        result = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=137, image="a")
        )
        await session.commit()

        app = FastAPI()
        install_problem_handlers(app)
        app.include_router(router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[require_principal] = lambda: Principal.for_user(
            user_id=uuid.uuid4(), subject="s", email="o@e.test", role=UserRole.ADMIN
        )
        # Nothing composed: no metrics tier, no log reader, no model. The honest outcome is
        # `insufficient_evidence` -- and critically NOT a conclusion.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(f"/incidents/{result.incident_id}/analysis")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "insufficient_evidence"
        assert body["problem"] == ""
        assert "not enough to identify a cause" in body["explanation"]

        # THE ROW IS READ BACK, not inferred from the response.
        row = (
            (
                await session.execute(
                    text("SELECT state, problem, evidence_consulted FROM incident_analyses WHERE incident_id = :id"),
                    {"id": result.incident_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["state"] == "insufficient_evidence"
        assert row["problem"] == ""
        # Both absent sources were COUNTED rather than skipped -- a skipped source would not count
        # against the evidence floor, and the floor is what refuses a one-source conclusion.
        assert row["evidence_consulted"] == 2

        evidence_kinds = [
            r[0]
            for r in (
                await session.execute(
                    text("SELECT kind FROM incident_evidence WHERE incident_id = :id ORDER BY kind"),
                    {"id": result.incident_id},
                )
            ).all()
        ]
        assert evidence_kinds == ["logs", "metrics"]

    async def test_a_second_analysis_appends_rather_than_replacing(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Overwriting would hide that the conclusion changed, and which evidence changed it."""
        findings = [_readable("metrics"), _readable("logs")]
        result = await ingest(
            session, from_container_exit(project_id=project_id, container="api", exit_code=1, image="a")
        )
        first = await rca.analyse(model=None, title="t", source="container_exit", detail={}, findings=findings)
        await rca.persist(session, incident_id=result.incident_id, findings=findings, analysis=first)
        second = await rca.analyse(
            model=_Model(
                _Completion(content="### PROBLEM\nOOM killed.\n### LOCATION\ndeploy/api.yaml\n### FIX\nraise it\n")
            ),
            title="t",
            source="container_exit",
            detail={},
            findings=findings,
        )
        await rca.persist(session, incident_id=result.incident_id, findings=findings, analysis=second)
        await session.commit()

        states = [
            r[0]
            for r in (
                await session.execute(
                    text("SELECT state FROM incident_analyses WHERE incident_id = :id ORDER BY created_at"),
                    {"id": result.incident_id},
                )
            ).all()
        ]
        assert states == ["unavailable", "analysed"]
