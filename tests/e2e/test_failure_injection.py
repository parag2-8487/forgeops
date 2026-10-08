"""Failure-injection tests verifying error classification, fast-fail, and source protection."""

import pytest
from backend.src.blueprint.models import BuildConfig, NetworkContract, ProjectBlueprint, RuntimeContract, WorkloadType
from backend.src.diagnostics.bundle import DiagnosticBundle
from backend.src.recovery.ai_resolver import resolve_failure_with_ai


def create_mock_blueprint() -> ProjectBlueprint:
    return ProjectBlueprint(
        blueprint_id="bp-fail-test",
        repository_root="/mock/repo",
        workload_type=WorkloadType.WEB_SERVICE,
        build_config=BuildConfig(),
        runtime=RuntimeContract(language="nodejs", runtime_version="20"),
        network=NetworkContract(listen_port=3000),
    )


def test_application_source_code_failure_halts_without_source_mutation():
    """ForgeOps must NEVER mutate application source code when runtime bugs occur."""
    bp = create_mock_blueprint()
    bundle = DiagnosticBundle(
        gate_identifier="G5",
        stage="startup",
        blueprint=bp,
        stderr="Traceback (most recent call last):\n  File 'app.py', line 12, in <module>\nZeroDivisionError: division by zero",
        error_classification="APPLICATION_CODE",
    )

    plan = resolve_failure_with_ai(bundle, current_iteration=1)
    assert plan.status == "HALTED_APPLICATION_CODE_DEFECT"
    assert plan.root_cause_category == "APPLICATION_CODE"
    assert "strictly prohibited from mutating application source code" in plan.operator_message
    assert len(plan.suggested_manifests) == 0


def test_ai_recovery_bounded_to_three_iterations():
    """Recovery must halt after iteration 3 to avoid infinite loops."""
    bp = create_mock_blueprint()
    bundle = DiagnosticBundle(
        gate_identifier="G4",
        stage="build",
        blueprint=bp,
        stderr="COPY failed: file not found",
        error_classification="DETERMINISTIC",
    )

    # Iteration 1 -> Proposes fix
    plan1 = resolve_failure_with_ai(bundle, current_iteration=1)
    assert plan1.status == "PROPOSED"

    # Iteration 4 -> Halts immediately
    plan4 = resolve_failure_with_ai(bundle, current_iteration=4)
    assert plan4.status == "HALTED_MAX_ITERATIONS"
    assert "maximum AI resolution limit" in plan4.operator_message
