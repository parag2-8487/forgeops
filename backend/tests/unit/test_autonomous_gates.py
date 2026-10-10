# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Canonical Gate Evaluators G1-G7 & Strategy Execution Graph (Phase 5).

Tests:
1. Individual canonical gate evaluations (G1-G7) and operational stages.
2. Strategy-specific pipeline execution across all 4 strategies:
   - docker_github_vercel (all 9 stages)
   - docker_github (8 stages, vercel omitted)
   - github_only (5 stages, docker G4-G6 & vercel omitted)
   - vercel_only (5 stages, docker G4-G6 & github omitted)
3. Strategy-aware G7 verification results:
   - Inactive targets explicitly marked 'not_applicable'
   - Active targets marked 'verified' or 'failed'
   - Inactive targets do not fail overall_passed
4. Pipeline failure handling:
   - Pipeline halts at the failing gate
   - Failing stage marked 'failed'
   - Subsequent stages remain 'pending'
   - Run marked 'failed' with error_summary and primary_error recorded
5. Cooperative cancellation during pipeline execution.
"""

from __future__ import annotations

import operator
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Update
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, UnaryExpression
from src.deployments.autonomous_gates import (
    G7VerificationResult,
    GateResult,
    evaluate_g1_blueprint,
    evaluate_g2_existing_artifacts,
    evaluate_g3_consistency,
    evaluate_g4_build,
    evaluate_g5_apply,
    evaluate_g6_workload,
    evaluate_g7_final_verification,
    execute_github_release,
    execute_vercel_deploy,
)
from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from src.deployments.autonomous_schemas import (
    DeploymentStrategy,
)
from src.deployments.autonomous_service import (
    STAGE_G1_BLUEPRINT,
    STAGE_G2_ARTIFACT,
    STAGE_G3_CONSISTENCY,
    STAGE_G4_BUILD,
    STAGE_G5_APPLY,
    STAGE_G6_WORKLOAD,
    STAGE_G7_VERIFICATION,
    STAGE_GITHUB_RELEASE,
    STAGE_VERCEL_DEPLOY,
    build_stage_graph,
)
from src.deployments.autonomous_worker import (
    AutonomousWorker,
    run_pipeline,
)


def _eval_clause(clause: Any, run: AutonomousDeployment) -> bool:
    """Evaluates an arbitrary SQLAlchemy whereclause AST against an in-memory AutonomousDeployment."""
    if clause is None:
        return True

    if hasattr(clause, "element") and not isinstance(clause, UnaryExpression):
        return _eval_clause(clause.element, run)

    if isinstance(clause, BooleanClauseList):
        if clause.operator is operator.or_:
            return any(_eval_clause(c, run) for c in clause.clauses)
        return all(_eval_clause(c, run) for c in clause.clauses)

    if isinstance(clause, BinaryExpression):
        left_name = getattr(clause.left, "name", None)
        run_val = getattr(run, left_name, None) if left_name else None
        right = clause.right
        val = getattr(right, "value", right)
        op_name = getattr(clause.operator, "__name__", "")

        if "is_not" in op_name:
            return run_val is not None
        if "is_" in op_name or op_name == "is":
            return run_val is None
        if "not_in" in op_name or "notin" in op_name:
            val_items = getattr(right, "value", right)
            return run_val not in val_items
        if "in_op" in op_name or "in_" in op_name:
            val_items = getattr(right, "value", right)
            return run_val in val_items

        if isinstance(run_val, datetime) and isinstance(val, datetime):
            if run_val.tzinfo is None and val.tzinfo is not None:
                run_val = run_val.replace(tzinfo=UTC)
            elif run_val.tzinfo is not None and val.tzinfo is None:
                val = val.replace(tzinfo=UTC)

        try:
            return bool(clause.operator(run_val, val))
        except Exception:
            return False

    if isinstance(clause, UnaryExpression):
        s = str(clause)
        if "IS NULL" in s:
            col_name = getattr(clause.element, "name", None)
            return getattr(run, col_name, None) is None
        if "IS NOT NULL" in s:
            col_name = getattr(clause.element, "name", None)
            return getattr(run, col_name, None) is not None

    return True


class MockScalars:
    """Mock scalars collection matching SQLAlchemy CursorResult."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any | None:
        return self._items[0] if self._items else None

    def all(self) -> list[Any]:
        return list(self._items)


class MockResult:
    """Mock SQLAlchemy execution result."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> MockScalars:
        return MockScalars(self._items)

    def scalar_one_or_none(self) -> Any | None:
        return self._items[0] if self._items else None

    def scalar(self) -> Any | None:
        return self._items[0] if self._items else None

    @property
    def rowcount(self) -> int:
        return len(self._items)


class MockAsyncSession:
    """In-memory AsyncSession supporting Select, Update, returning, and child relationships."""

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, AutonomousDeployment] = {}
        self.stages: list[AutonomousDeploymentStage] = []
        self.logs: list[AutonomousDeploymentLog] = []
        self.outbox: list[AutonomousDeploymentOutbox] = []

    def add(self, entity: Any) -> None:
        if isinstance(entity, AutonomousDeployment):
            self.runs[entity.id] = entity
        elif isinstance(entity, AutonomousDeploymentStage):
            if entity not in self.stages:
                self.stages.append(entity)
        elif isinstance(entity, AutonomousDeploymentLog):
            if entity not in self.logs:
                self.logs.append(entity)
        elif isinstance(entity, AutonomousDeploymentOutbox):
            if entity not in self.outbox:
                self.outbox.append(entity)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def execute(self, stmt: Any) -> MockResult:
        if isinstance(stmt, Update) or (hasattr(stmt, "is_update") and stmt.is_update):
            matching_runs: list[AutonomousDeployment] = []
            for run in self.runs.values():
                if _eval_clause(stmt.whereclause, run):
                    matching_runs.append(run)

            for run in matching_runs:
                for k, v in stmt._values.items():
                    col_name = k.name
                    if isinstance(v, BinaryExpression):
                        left_name = getattr(v.left, "name", col_name)
                        left_val = getattr(run, left_name)
                        right_val = getattr(v.right, "value", v.right)
                        new_val = v.operator(left_val, right_val)
                    elif hasattr(v, "value"):
                        new_val = v.value
                    else:
                        new_val = v
                    setattr(run, col_name, new_val)

            if getattr(stmt, "_returning", None):
                return_col = stmt._returning[0]
                ret_vals = [getattr(r, return_col.name) for r in matching_runs]
                return MockResult(ret_vals)

            return MockResult([r.id for r in matching_runs])

        entity_cls = stmt.column_descriptions[0]["type"]

        if entity_cls is AutonomousDeployment:
            candidates = list(self.runs.values())
            for r in candidates:
                if not r.stages:
                    r.stages = [s for s in self.stages if s.run_id == r.id]
                    r.stages.sort(key=lambda s: s.position)

            matched = [r for r in candidates if _eval_clause(stmt.whereclause, r)]
            return MockResult(matched)

        elif entity_cls is AutonomousDeploymentStage:
            matched_stages = [
                s
                for s in self.stages
                if (
                    not hasattr(stmt, "whereclause") or stmt.whereclause is None or _eval_clause(stmt.whereclause, s)  # type: ignore[arg-type]
                )
            ]
            matched_stages.sort(key=lambda s: s.position)
            return MockResult(matched_stages)

        elif entity_cls is AutonomousDeploymentLog:
            matched_logs = list(self.logs)
            if hasattr(stmt, "whereclause") and stmt.whereclause is not None:
                matched_logs = [log for log in matched_logs if _eval_clause(stmt.whereclause, log)]  # type: ignore[arg-type]
            matched_logs.sort(key=lambda entry: entry.log_seq)
            return MockResult(matched_logs)

        return MockResult([])


async def _create_test_run(
    session: MockAsyncSession,
    strategy: DeploymentStrategy,
    status: str = "pending",
    config_overrides: dict[str, Any] | None = None,
) -> AutonomousDeployment:
    """Helper to initialize an autonomous deployment run with strategy-appropriate stages."""
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()

    default_config: dict[str, Any] = {"strategy": strategy.value}
    if strategy in (
        DeploymentStrategy.DOCKER_GITHUB_VERCEL,
        DeploymentStrategy.DOCKER_GITHUB,
        DeploymentStrategy.GITHUB_ONLY,
    ):
        default_config["github_config"] = {
            "repository_name": "forgeops/test-repo",
            "target_branch": "main",
            "commit_message": "Automated deployment by ForgeOps",
        }
    if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.VERCEL_ONLY):
        default_config["vercel_config"] = {
            "project_name": "forgeops-app",
            "production_deploy": True,
        }
    if strategy in (DeploymentStrategy.DOCKER_GITHUB_VERCEL, DeploymentStrategy.DOCKER_GITHUB):
        default_config["docker_config"] = {
            "port_bindings": {"8080": 8080},
            "container_name": "forgeops-test-cnt",
        }

    if config_overrides:
        default_config.update(config_overrides)

    run = AutonomousDeployment(
        id=run_id,
        project_id=project_id,
        status=status,
        strategy=strategy.value,
        configuration=default_config,
        progress_pct=0,
        payload_hash="dummy_hash_for_testing",
        created_by=uuid.uuid4(),
        dispatch_status="pending",
        fence_token=0,
        log_sequence_counter=0,
        outbox_sequence_counter=1,
    )
    session.add(run)

    stage_defs = build_stage_graph(strategy)
    stages: list[AutonomousDeploymentStage] = []
    for pos, (s_name, g_id) in enumerate(stage_defs, start=1):
        st = AutonomousDeploymentStage(
            id=uuid.uuid4(),
            run_id=run.id,
            stage_name=s_name,
            gate_id=g_id,
            position=pos,
            status="pending",
            progress_pct=0,
            stage_metadata={},
        )
        session.add(st)
        stages.append(st)

    run.stages = stages
    await session.flush()
    return run


# ===========================================================================
# 1. Individual Canonical Gate Evaluation Tests (G1-G7)
# ===========================================================================


class TestIndividualGateEvaluations:
    """Verifies each canonical gate evaluator G1-G7 individually."""

    @pytest.mark.asyncio
    async def test_evaluate_g1_blueprint_success(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB_VERCEL)
        result = await evaluate_g1_blueprint(session, run)

        assert isinstance(result, GateResult)
        assert result.gate_id == "G1"
        assert result.passed is True
        assert result.status == "succeeded"
        assert "validated successfully" in result.message

    @pytest.mark.asyncio
    async def test_evaluate_g1_blueprint_missing_required_config(self) -> None:
        session = MockAsyncSession()
        # Strategy requires vercel_config, omit it
        run = await _create_test_run(
            session,
            DeploymentStrategy.DOCKER_GITHUB_VERCEL,
            config_overrides={"vercel_config": None},
        )
        result = await evaluate_g1_blueprint(session, run)

        assert result.passed is False
        assert result.status == "failed"
        assert "Missing required vercel_config" in result.message

    @pytest.mark.asyncio
    async def test_evaluate_g1_blueprint_context_failure_override(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.GITHUB_ONLY)
        result = await evaluate_g1_blueprint(session, run, context={"fail_g1": True})

        assert result.passed is False
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_evaluate_g2_existing_artifacts_reuse(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)
        context = {"reusable_dockerfile": "Dockerfile.production"}
        result = await evaluate_g2_existing_artifacts(session, run, context)

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["action"] == "REUSE"
        assert result.details["reusable_dockerfile"] == "Dockerfile.production"

    @pytest.mark.asyncio
    async def test_evaluate_g2_existing_artifacts_synthesize_default(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)
        result = await evaluate_g2_existing_artifacts(session, run)

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["action"] == "SYNTHESIZE"

    @pytest.mark.asyncio
    async def test_evaluate_g2_existing_artifacts_not_required_cloud_only(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.VERCEL_ONLY)
        result = await evaluate_g2_existing_artifacts(session, run)

        assert result.passed is True
        assert result.status == "succeeded"
        assert result.details["action"] == "NOT_REQUIRED"

    @pytest.mark.asyncio
    async def test_evaluate_g2_existing_artifacts_failure_override(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)
        result = await evaluate_g2_existing_artifacts(session, run, context={"fail_g2": True})

        assert result.passed is False
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_evaluate_g3_consistency_success(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB_VERCEL)
        result = await evaluate_g3_consistency(session, run)

        assert result.passed is True
        assert result.status == "succeeded"

    @pytest.mark.asyncio
    async def test_evaluate_g3_consistency_reserved_env_override(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(
            session,
            DeploymentStrategy.DOCKER_GITHUB,
            config_overrides={
                "docker_config": {
                    "environment_overrides": {"PORT": "9000"},
                }
            },
        )
        result = await evaluate_g3_consistency(session, run)

        assert result.passed is False
        assert result.status == "failed"
        assert any("Reserved environment variable" in err for err in result.details.get("errors", []))

    @pytest.mark.asyncio
    async def test_evaluate_g3_consistency_invalid_github_repo(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(
            session,
            DeploymentStrategy.GITHUB_ONLY,
            config_overrides={"github_config": {"repository_name": "invalidrepo", "target_branch": "main"}},
        )
        result = await evaluate_g3_consistency(session, run)

        assert result.passed is False
        assert result.status == "failed"
        assert any("expected 'owner/repo'" in err for err in result.details.get("errors", []))

    @pytest.mark.asyncio
    async def test_evaluate_g4_build_success_and_failure(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)

        res_ok = await evaluate_g4_build(session, run)
        assert res_ok.passed is True
        assert res_ok.status == "succeeded"
        assert "image_tag" in res_ok.details

        res_fail = await evaluate_g4_build(session, run, context={"fail_g4": True})
        assert res_fail.passed is False
        assert res_fail.status == "failed"

    @pytest.mark.asyncio
    async def test_evaluate_g5_apply_success_and_failure(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)

        res_ok = await evaluate_g5_apply(session, run)
        assert res_ok.passed is True
        assert res_ok.status == "succeeded"
        assert "container_id" in res_ok.details

        res_fail = await evaluate_g5_apply(session, run, context={"fail_g5": True})
        assert res_fail.passed is False
        assert res_fail.status == "failed"

    @pytest.mark.asyncio
    async def test_evaluate_g6_workload_success_and_failure(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)

        res_ok = await evaluate_g6_workload(session, run)
        assert res_ok.passed is True
        assert res_ok.status == "succeeded"
        assert res_ok.details.get("http_status") == 200

        res_fail = await evaluate_g6_workload(session, run, context={"fail_g6": True})
        assert res_fail.passed is False
        assert res_fail.status == "failed"

    @pytest.mark.asyncio
    async def test_operational_stages_github_and_vercel(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB_VERCEL)

        gh_ok = await execute_github_release(session, run)
        assert gh_ok.passed is True
        assert "commit_sha" in gh_ok.details

        gh_fail = await execute_github_release(session, run, context={"fail_github": True})
        assert gh_fail.passed is False

        v_ok = await execute_vercel_deploy(session, run)
        assert v_ok.passed is True
        assert "deployment_url" in v_ok.details

        v_fail = await execute_vercel_deploy(session, run, context={"fail_vercel": True})
        assert v_fail.passed is False


# ===========================================================================
# 2. Strategy-Aware G7 Final Verification Tests
# ===========================================================================


class TestStrategyAwareG7Verification:
    """Verifies G7 target-by-target inspection and strategy awareness."""

    @pytest.mark.asyncio
    async def test_g7_docker_github_vercel_all_active_verified(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB_VERCEL)
        result = await evaluate_g7_final_verification(session, run)

        assert isinstance(result, G7VerificationResult)
        assert result.gate_id == "G7"
        assert result.overall_passed is True
        assert result.passed is True
        assert result.target_results["docker"] == "verified"
        assert result.target_results["github"] == "verified"
        assert result.target_results["vercel"] == "verified"

    @pytest.mark.asyncio
    async def test_g7_docker_github_strategy_omits_vercel(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)
        result = await evaluate_g7_final_verification(session, run)

        assert result.overall_passed is True
        assert result.target_results["docker"] == "verified"
        assert result.target_results["github"] == "verified"
        # Inactive target is explicitly marked not_applicable
        assert result.target_results["vercel"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_g7_github_only_strategy_omits_docker_and_vercel(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.GITHUB_ONLY)
        result = await evaluate_g7_final_verification(session, run)

        assert result.overall_passed is True
        assert result.target_results["docker"] == "not_applicable"
        assert result.target_results["github"] == "verified"
        assert result.target_results["vercel"] == "not_applicable"

    @pytest.mark.asyncio
    async def test_g7_vercel_only_strategy_omits_docker_and_github(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.VERCEL_ONLY)
        result = await evaluate_g7_final_verification(session, run)

        assert result.overall_passed is True
        assert result.target_results["docker"] == "not_applicable"
        assert result.target_results["github"] == "not_applicable"
        assert result.target_results["vercel"] == "verified"

    @pytest.mark.asyncio
    async def test_g7_active_target_failure_marks_overall_passed_false(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)
        # Fail docker target specifically
        result = await evaluate_g7_final_verification(session, run, context={"fail_docker": True})

        assert result.overall_passed is False
        assert result.passed is False
        assert result.target_results["docker"] == "failed"
        assert result.target_results["github"] == "verified"
        assert result.target_results["vercel"] == "not_applicable"
        assert "docker" in result.message

    @pytest.mark.asyncio
    async def test_g7_inactive_target_does_not_fail_overall_passed(self) -> None:
        session = MockAsyncSession()
        # Even if fail_docker is requested, if strategy is vercel_only, docker is not_applicable
        run = await _create_test_run(session, DeploymentStrategy.VERCEL_ONLY)
        result = await evaluate_g7_final_verification(session, run, context={"fail_docker": True})

        assert result.overall_passed is True
        assert result.target_results["docker"] == "not_applicable"
        assert result.target_results["vercel"] == "verified"


# ===========================================================================
# 3. Strategy-Specific Sequential Pipeline Execution Tests
# ===========================================================================


class TestStrategyPipelineExecution:
    """Tests sequential stage execution across all 4 deployment strategies."""

    @pytest.mark.asyncio
    async def test_pipeline_docker_github_vercel_full_success(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB_VERCEL)

        completed_run = await run_pipeline(session, run_id=run.id, worker_id="test-worker-1")

        assert completed_run.status == "succeeded"
        assert completed_run.progress_pct == 100
        assert completed_run.completed_at is not None

        # Verify all 9 stages succeeded
        stage_names = [s.stage_name for s in completed_run.stages]
        assert len(stage_names) == 9
        assert stage_names == [
            STAGE_G1_BLUEPRINT,
            STAGE_G2_ARTIFACT,
            STAGE_G3_CONSISTENCY,
            STAGE_G4_BUILD,
            STAGE_G5_APPLY,
            STAGE_G6_WORKLOAD,
            STAGE_GITHUB_RELEASE,
            STAGE_VERCEL_DEPLOY,
            STAGE_G7_VERIFICATION,
        ]
        assert all(s.status == "succeeded" for s in completed_run.stages)
        assert all(s.progress_pct == 100 for s in completed_run.stages)

        # Verify logs were appended
        assert len(session.logs) > 0
        assert any(log.stage_name == STAGE_G7_VERIFICATION for log in session.logs)

    @pytest.mark.asyncio
    async def test_pipeline_docker_github_success_omits_vercel(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)

        completed_run = await run_pipeline(session, run_id=run.id, worker_id="test-worker-2")

        assert completed_run.status == "succeeded"
        assert completed_run.progress_pct == 100

        stage_names = [s.stage_name for s in completed_run.stages]
        assert len(stage_names) == 8
        assert STAGE_VERCEL_DEPLOY not in stage_names
        assert all(s.status == "succeeded" for s in completed_run.stages)

    @pytest.mark.asyncio
    async def test_pipeline_github_only_success_omits_docker_stages(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.GITHUB_ONLY)

        completed_run = await run_pipeline(session, run_id=run.id, worker_id="test-worker-3")

        assert completed_run.status == "succeeded"
        assert completed_run.progress_pct == 100

        stage_names = [s.stage_name for s in completed_run.stages]
        assert len(stage_names) == 5
        assert stage_names == [
            STAGE_G1_BLUEPRINT,
            STAGE_G2_ARTIFACT,
            STAGE_G3_CONSISTENCY,
            STAGE_GITHUB_RELEASE,
            STAGE_G7_VERIFICATION,
        ]
        # Ensure G4-G6 and Vercel are omitted
        assert STAGE_G4_BUILD not in stage_names
        assert STAGE_G5_APPLY not in stage_names
        assert STAGE_G6_WORKLOAD not in stage_names
        assert STAGE_VERCEL_DEPLOY not in stage_names
        assert all(s.status == "succeeded" for s in completed_run.stages)

    @pytest.mark.asyncio
    async def test_pipeline_vercel_only_success_omits_docker_and_github(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.VERCEL_ONLY)

        completed_run = await run_pipeline(session, run_id=run.id, worker_id="test-worker-4")

        assert completed_run.status == "succeeded"
        assert completed_run.progress_pct == 100

        stage_names = [s.stage_name for s in completed_run.stages]
        assert len(stage_names) == 5
        assert stage_names == [
            STAGE_G1_BLUEPRINT,
            STAGE_G2_ARTIFACT,
            STAGE_G3_CONSISTENCY,
            STAGE_VERCEL_DEPLOY,
            STAGE_G7_VERIFICATION,
        ]
        assert STAGE_G4_BUILD not in stage_names
        assert STAGE_GITHUB_RELEASE not in stage_names
        assert all(s.status == "succeeded" for s in completed_run.stages)


# ===========================================================================
# 4. Pipeline Gate Failure Handling & State Isolation Tests
# ===========================================================================


class TestPipelineFailureHandling:
    """Verifies that gate failures stop the pipeline cleanly and leave downstream stages pending."""

    @pytest.mark.asyncio
    async def test_pipeline_halts_at_failing_g3_consistency_gate(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)

        # Context forces G3 failure
        failed_run = await run_pipeline(
            session,
            run_id=run.id,
            worker_id="test-worker-fail-1",
            context={"fail_g3": True},
        )

        assert failed_run.status == "failed"
        assert failed_run.error_summary is not None
        assert "consistency" in failed_run.error_summary.lower()
        assert failed_run.primary_error is not None
        assert failed_run.primary_error["stage_name"] == STAGE_G3_CONSISTENCY
        assert failed_run.primary_error["gate_id"] == "G3"

        # Check stages: G1 and G2 succeeded, G3 failed, subsequent stages pending
        stages_by_name = {s.stage_name: s for s in failed_run.stages}
        assert stages_by_name[STAGE_G1_BLUEPRINT].status == "succeeded"
        assert stages_by_name[STAGE_G2_ARTIFACT].status == "succeeded"
        assert stages_by_name[STAGE_G3_CONSISTENCY].status == "failed"

        # Subsequent stages remain pending
        assert stages_by_name[STAGE_G4_BUILD].status == "pending"
        assert stages_by_name[STAGE_G5_APPLY].status == "pending"
        assert stages_by_name[STAGE_G6_WORKLOAD].status == "pending"
        assert stages_by_name[STAGE_GITHUB_RELEASE].status == "pending"
        assert stages_by_name[STAGE_G7_VERIFICATION].status == "pending"

    @pytest.mark.asyncio
    async def test_pipeline_halts_at_failing_g4_build_gate(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB_VERCEL)

        failed_run = await run_pipeline(
            session,
            run_id=run.id,
            worker_id="test-worker-fail-2",
            context={"fail_g4": True},
        )

        assert failed_run.status == "failed"
        assert failed_run.primary_error["stage_name"] == STAGE_G4_BUILD
        assert failed_run.primary_error["gate_id"] == "G4"

        stages_by_name = {s.stage_name: s for s in failed_run.stages}
        assert stages_by_name[STAGE_G1_BLUEPRINT].status == "succeeded"
        assert stages_by_name[STAGE_G2_ARTIFACT].status == "succeeded"
        assert stages_by_name[STAGE_G3_CONSISTENCY].status == "succeeded"
        assert stages_by_name[STAGE_G4_BUILD].status == "failed"

        # Remaining stages must stay pending
        assert stages_by_name[STAGE_G5_APPLY].status == "pending"
        assert stages_by_name[STAGE_G6_WORKLOAD].status == "pending"
        assert stages_by_name[STAGE_GITHUB_RELEASE].status == "pending"
        assert stages_by_name[STAGE_VERCEL_DEPLOY].status == "pending"
        assert stages_by_name[STAGE_G7_VERIFICATION].status == "pending"

    @pytest.mark.asyncio
    async def test_pipeline_halts_at_failing_g7_final_verification(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.GITHUB_ONLY)

        failed_run = await run_pipeline(
            session,
            run_id=run.id,
            worker_id="test-worker-fail-3",
            context={"fail_g7": True},
        )

        assert failed_run.status == "failed"
        assert failed_run.primary_error["stage_name"] == STAGE_G7_VERIFICATION
        assert failed_run.primary_error["gate_id"] == "G7"

        stages_by_name = {s.stage_name: s for s in failed_run.stages}
        assert stages_by_name[STAGE_G1_BLUEPRINT].status == "succeeded"
        assert stages_by_name[STAGE_G2_ARTIFACT].status == "succeeded"
        assert stages_by_name[STAGE_G3_CONSISTENCY].status == "succeeded"
        assert stages_by_name[STAGE_GITHUB_RELEASE].status == "succeeded"
        assert stages_by_name[STAGE_G7_VERIFICATION].status == "failed"

        # Progress cannot reach 100 when G7 failed
        assert failed_run.progress_pct < 100

    @pytest.mark.asyncio
    async def test_pipeline_cooperative_cancellation_settlement(self) -> None:
        session = MockAsyncSession()
        run = await _create_test_run(session, DeploymentStrategy.DOCKER_GITHUB)

        # Transition run to cancelling before pipeline runs
        run.status = "cancelling"

        worker = AutonomousWorker(worker_id="test-worker-cancel")
        cancelled_run = await worker.run_pipeline(session, run_id=run.id)

        assert cancelled_run.status == "cancelled"
        assert cancelled_run.completed_at is not None
