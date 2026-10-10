# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for Autonomous Deployment ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from src.deployments.autonomous_models import (
    AutonomousDeployment,
    AutonomousDeploymentLog,
    AutonomousDeploymentOutbox,
    AutonomousDeploymentStage,
)
from src.deployments.models import (
    AutonomousDeployment as ReexportedDeployment,
    AutonomousDeploymentLog as ReexportedLog,
    AutonomousDeploymentOutbox as ReexportedOutbox,
    AutonomousDeploymentStage as ReexportedStage,
)


def test_reexports_match():
    """Ensure models are correctly re-exported from src.deployments.models."""
    assert ReexportedDeployment is AutonomousDeployment
    assert ReexportedStage is AutonomousDeploymentStage
    assert ReexportedLog is AutonomousDeploymentLog
    assert ReexportedOutbox is AutonomousDeploymentOutbox


def test_autonomous_deployment_instantiation_and_defaults():
    """Verify default values and instantiation of AutonomousDeployment."""
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()

    run = AutonomousDeployment(
        project_id=project_id,
        strategy="docker_only",
        payload_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        created_by=user_id,
    )

    assert isinstance(run.id, uuid.UUID)
    assert run.project_id == project_id
    assert run.parent_run_id is None
    assert run.attempt_number == 1
    assert run.status == "pending"
    assert run.strategy == "docker_only"
    assert run.configuration == {}
    assert run.progress_pct == 0
    assert run.current_stage is None
    assert run.error_summary is None
    assert run.primary_error is None
    assert run.compensation_error is None
    assert run.worker_id is None
    assert run.fence_token == 0
    assert run.lease_expires_at is None
    assert run.dispatch_status == "pending"
    assert run.dispatch_requested_at is None
    assert run.idempotency_key is None
    assert run.payload_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert run.log_sequence_counter == 0
    assert run.outbox_sequence_counter == 0
    assert run.created_by == user_id
    assert run.started_at is None
    assert run.completed_at is None


def test_autonomous_deployment_stage_instantiation_and_defaults():
    """Verify default values and instantiation of AutonomousDeploymentStage."""
    run_id = uuid.uuid4()

    stage = AutonomousDeploymentStage(
        run_id=run_id,
        stage_name="G1_blueprint",
        position=1,
    )

    assert isinstance(stage.id, uuid.UUID)
    assert stage.run_id == run_id
    assert stage.stage_name == "G1_blueprint"
    assert stage.gate_id is None
    assert stage.position == 1
    assert stage.status == "pending"
    assert stage.progress_pct == 0
    assert stage.started_at is None
    assert stage.completed_at is None
    assert stage.error_message is None
    assert stage.stage_metadata == {}

    # Verify metadata alias in constructor
    custom_stage = AutonomousDeploymentStage(
        run_id=run_id,
        stage_name="G2_artifact",
        position=2,
        metadata={"custom": "val"},
    )
    assert custom_stage.stage_metadata == {"custom": "val"}


def test_autonomous_deployment_log_instantiation_and_defaults():
    """Verify default values and instantiation of AutonomousDeploymentLog."""
    run_id = uuid.uuid4()

    log = AutonomousDeploymentLog(
        run_id=run_id,
        stage_name="G1_blueprint",
        log_seq=1,
        message="Validating blueprint schema",
    )

    assert log.id is None  # BigInteger primary key assigned by db
    assert log.run_id == run_id
    assert log.stage_name == "G1_blueprint"
    assert log.log_seq == 1
    assert log.level == "INFO"
    assert log.message == "Validating blueprint schema"


def test_autonomous_deployment_outbox_instantiation_and_defaults():
    """Verify default values and instantiation of AutonomousDeploymentOutbox."""
    run_id = uuid.uuid4()

    outbox = AutonomousDeploymentOutbox(
        run_id=run_id,
        event_seq=1,
        event_type="stage_started",
        payload={"stage": "G1_blueprint"},
    )

    assert outbox.id is None  # BigInteger primary key assigned by db
    assert outbox.run_id == run_id
    assert outbox.event_seq == 1
    assert outbox.event_type == "stage_started"
    assert outbox.payload == {"stage": "G1_blueprint"}
    assert outbox.status == "pending"


def test_model_relationships_and_back_populates():
    """Verify in-memory relationship wiring between run and child records."""
    run = AutonomousDeployment(
        project_id=uuid.uuid4(),
        strategy="docker_only",
        payload_hash="hash123",
        created_by=uuid.uuid4(),
    )

    stage = AutonomousDeploymentStage(
        run_id=run.id,
        stage_name="G1_blueprint",
        position=1,
    )
    log = AutonomousDeploymentLog(
        run_id=run.id,
        stage_name="G1_blueprint",
        log_seq=1,
        message="Running gate G1",
    )
    outbox = AutonomousDeploymentOutbox(
        run_id=run.id,
        event_seq=1,
        event_type="run_started",
        payload={"status": "running"},
    )

    run.stages.append(stage)
    run.logs.append(log)
    run.outbox_events.append(outbox)

    assert len(run.stages) == 1
    assert run.stages[0] is stage
    assert stage.run is run

    assert len(run.logs) == 1
    assert run.logs[0] is log
    assert log.run is run

    assert len(run.outbox_events) == 1
    assert run.outbox_events[0] is outbox
    assert outbox.run is run


def test_table_schema_and_constraints_definitions():
    """Verify that ORM __table__ definitions match specification exactly."""
    # 1. autonomous_deployments table
    t_dep = AutonomousDeployment.__table__
    assert t_dep.name == "autonomous_deployments"

    dep_col_types = {c.name: type(c.type) for c in t_dep.columns}
    assert dep_col_types["id"] is Uuid
    assert dep_col_types["project_id"] is Uuid
    assert dep_col_types["parent_run_id"] is Uuid
    assert dep_col_types["attempt_number"] is Integer
    assert dep_col_types["status"] is String
    assert dep_col_types["strategy"] is String
    assert dep_col_types["configuration"] is JSONB
    assert dep_col_types["progress_pct"] is Integer
    assert dep_col_types["current_stage"] is String
    assert dep_col_types["error_summary"] is Text
    assert dep_col_types["primary_error"] is JSONB
    assert dep_col_types["compensation_error"] is JSONB
    assert dep_col_types["worker_id"] is String
    assert dep_col_types["fence_token"] is BigInteger
    assert dep_col_types["lease_expires_at"] is DateTime
    assert dep_col_types["dispatch_status"] is String
    assert dep_col_types["dispatch_requested_at"] is DateTime
    assert dep_col_types["idempotency_key"] is String
    assert dep_col_types["payload_hash"] is String
    assert dep_col_types["log_sequence_counter"] is Integer
    assert dep_col_types["outbox_sequence_counter"] is Integer
    assert dep_col_types["created_by"] is Uuid
    assert dep_col_types["created_at"] is DateTime
    assert dep_col_types["started_at"] is DateTime
    assert dep_col_types["completed_at"] is DateTime

    dep_constraints = {c.name: c for c in t_dep.constraints if c.name}
    assert "uq_proj_idempotency" in dep_constraints
    assert "chk_run_status" in dep_constraints
    assert "chk_progress_range" in dep_constraints

    dep_indexes = {idx.name: idx for idx in t_dep.indexes}
    assert "idx_auto_deploy_proj_status" in dep_indexes
    assert "idx_auto_deploy_lease" in dep_indexes

    # 2. autonomous_deployment_stages table
    t_stage = AutonomousDeploymentStage.__table__
    assert t_stage.name == "autonomous_deployment_stages"

    stage_col_types = {c.name: type(c.type) for c in t_stage.columns}
    assert stage_col_types["id"] is Uuid
    assert stage_col_types["run_id"] is Uuid
    assert stage_col_types["stage_name"] is String
    assert stage_col_types["gate_id"] is String
    assert stage_col_types["position"] is Integer
    assert stage_col_types["status"] is String
    assert stage_col_types["progress_pct"] is Integer
    assert stage_col_types["started_at"] is DateTime
    assert stage_col_types["completed_at"] is DateTime
    assert stage_col_types["error_message"] is Text
    assert stage_col_types["metadata"] is JSONB
    assert stage_col_types["created_at"] is DateTime

    stage_constraints = {c.name: c for c in t_stage.constraints if c.name}
    assert "uq_run_stage" in stage_constraints
    assert "chk_stage_status" in stage_constraints

    stage_indexes = {idx.name: idx for idx in t_stage.indexes}
    assert "idx_auto_deploy_stages_run" in stage_indexes

    # 3. autonomous_deployment_logs table
    t_log = AutonomousDeploymentLog.__table__
    assert t_log.name == "autonomous_deployment_logs"

    log_col_types = {c.name: type(c.type) for c in t_log.columns}
    assert log_col_types["id"] is BigInteger
    assert log_col_types["run_id"] is Uuid
    assert log_col_types["stage_name"] is String
    assert log_col_types["log_seq"] is Integer
    assert log_col_types["level"] is String
    assert log_col_types["message"] is Text
    assert log_col_types["created_at"] is DateTime

    log_constraints = {c.name: c for c in t_log.constraints if c.name}
    assert "uq_run_log_seq" in log_constraints
    assert "chk_log_level" in log_constraints

    log_indexes = {idx.name: idx for idx in t_log.indexes}
    assert "idx_auto_deploy_logs_query" in log_indexes

    # 4. autonomous_deployment_outbox table
    t_outbox = AutonomousDeploymentOutbox.__table__
    assert t_outbox.name == "autonomous_deployment_outbox"

    outbox_col_types = {c.name: type(c.type) for c in t_outbox.columns}
    assert outbox_col_types["id"] is BigInteger
    assert outbox_col_types["run_id"] is Uuid
    assert outbox_col_types["event_seq"] is Integer
    assert outbox_col_types["event_type"] is String
    assert outbox_col_types["payload"] is JSONB
    assert outbox_col_types["status"] is String
    assert outbox_col_types["created_at"] is DateTime

    outbox_constraints = {c.name: c for c in t_outbox.constraints if c.name}
    assert "uq_run_outbox_seq" in outbox_constraints
    assert "chk_outbox_status" in outbox_constraints

    outbox_indexes = {idx.name: idx for idx in t_outbox.indexes}
    assert "idx_auto_deploy_outbox_pending" in outbox_indexes
