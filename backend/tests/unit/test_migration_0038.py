# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit test for Alembic migration 0038: Autonomous Deployments schema."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa


@pytest.fixture
def migration_0038():
    """Import and return the 0038 migration module."""
    try:
        return importlib.import_module("alembic.versions.0038_autonomous_deployments")
    except ModuleNotFoundError:
        # If running from backend/ or root, handle relative package paths
        import sys
        from pathlib import Path

        backend_dir = Path(__file__).resolve().parents[2]
        alembic_dir = backend_dir / "alembic" / "versions"
        if str(alembic_dir) not in sys.path:
            sys.path.insert(0, str(alembic_dir))
        return importlib.import_module("0038_autonomous_deployments")


def test_migration_metadata(migration_0038):
    """Verify revision identifiers chain correctly to 0037."""
    assert migration_0038.revision == "0038"
    assert migration_0038.down_revision == "0037"
    assert migration_0038.branch_labels is None
    assert migration_0038.depends_on is None


def test_migration_upgrade_and_downgrade_recording(migration_0038, monkeypatch):
    """Record op calls during upgrade and downgrade to verify schema actions."""
    created_tables: dict[str, list[sa.Column]] = {}
    table_constraints: dict[str, list[sa.Constraint]] = {}
    created_indexes: list[dict] = []
    dropped_tables: list[str] = []
    dropped_indexes: list[dict] = []

    mock_op = MagicMock()

    def fake_create_table(name: str, *elements, **kwargs):
        cols = [e for e in elements if isinstance(e, sa.Column)]
        cons = [e for e in elements if isinstance(e, sa.Constraint | sa.Index)]
        created_tables[name] = cols
        table_constraints[name] = cons
        return None

    def fake_create_index(name: str, table_name: str, columns: list[str], **kwargs):
        created_indexes.append({"name": name, "table_name": table_name, "columns": columns, **kwargs})
        return None

    def fake_drop_table(name: str, **kwargs):
        dropped_tables.append(name)
        return None

    def fake_drop_index(name: str, table_name: str | None = None, **kwargs):
        dropped_indexes.append({"name": name, "table_name": table_name, **kwargs})
        return None

    mock_op.create_table.side_effect = fake_create_table
    mock_op.create_index.side_effect = fake_create_index
    mock_op.drop_table.side_effect = fake_drop_table
    mock_op.drop_index.side_effect = fake_drop_index

    monkeypatch.setattr(migration_0038, "op", mock_op)

    # 1. Run upgrade
    migration_0038.upgrade()

    expected_tables = {
        "autonomous_deployments",
        "autonomous_deployment_stages",
        "autonomous_deployment_logs",
        "autonomous_deployment_outbox",
    }
    assert set(created_tables.keys()) == expected_tables

    # --- Verify autonomous_deployments ---
    deploy_cols = {c.name: c for c in created_tables["autonomous_deployments"]}
    assert "id" in deploy_cols and deploy_cols["id"].primary_key is True
    assert "project_id" in deploy_cols and not deploy_cols["project_id"].nullable
    assert "parent_run_id" in deploy_cols and deploy_cols["parent_run_id"].nullable
    assert "attempt_number" in deploy_cols and not deploy_cols["attempt_number"].nullable
    assert "status" in deploy_cols and not deploy_cols["status"].nullable
    assert "strategy" in deploy_cols and not deploy_cols["strategy"].nullable
    assert "configuration" in deploy_cols and not deploy_cols["configuration"].nullable
    assert "progress_pct" in deploy_cols and not deploy_cols["progress_pct"].nullable
    assert "current_stage" in deploy_cols and deploy_cols["current_stage"].nullable
    assert "error_summary" in deploy_cols and deploy_cols["error_summary"].nullable
    assert "primary_error" in deploy_cols and deploy_cols["primary_error"].nullable
    assert "compensation_error" in deploy_cols and deploy_cols["compensation_error"].nullable
    assert "worker_id" in deploy_cols and deploy_cols["worker_id"].nullable
    assert "fence_token" in deploy_cols and not deploy_cols["fence_token"].nullable
    assert "lease_expires_at" in deploy_cols and deploy_cols["lease_expires_at"].nullable
    assert "dispatch_status" in deploy_cols and not deploy_cols["dispatch_status"].nullable
    assert "dispatch_requested_at" in deploy_cols and deploy_cols["dispatch_requested_at"].nullable
    assert "idempotency_key" in deploy_cols and deploy_cols["idempotency_key"].nullable
    assert "payload_hash" in deploy_cols and not deploy_cols["payload_hash"].nullable
    assert "log_sequence_counter" in deploy_cols and not deploy_cols["log_sequence_counter"].nullable
    assert "outbox_sequence_counter" in deploy_cols and not deploy_cols["outbox_sequence_counter"].nullable
    assert "created_by" in deploy_cols and not deploy_cols["created_by"].nullable
    assert "created_at" in deploy_cols and not deploy_cols["created_at"].nullable
    assert "started_at" in deploy_cols and deploy_cols["started_at"].nullable
    assert "completed_at" in deploy_cols and deploy_cols["completed_at"].nullable

    deploy_constraints = {c.name: c for c in table_constraints["autonomous_deployments"]}
    assert "uq_proj_idempotency" in deploy_constraints
    assert "chk_run_status" in deploy_constraints
    assert "chk_progress_range" in deploy_constraints

    # --- Verify autonomous_deployment_stages ---
    stage_cols = {c.name: c for c in created_tables["autonomous_deployment_stages"]}
    assert "id" in stage_cols and stage_cols["id"].primary_key is True
    assert "run_id" in stage_cols and not stage_cols["run_id"].nullable
    assert "stage_name" in stage_cols and not stage_cols["stage_name"].nullable
    assert "gate_id" in stage_cols and stage_cols["gate_id"].nullable
    assert "position" in stage_cols and not stage_cols["position"].nullable
    assert "status" in stage_cols and not stage_cols["status"].nullable
    assert "progress_pct" in stage_cols and not stage_cols["progress_pct"].nullable
    assert "started_at" in stage_cols and stage_cols["started_at"].nullable
    assert "completed_at" in stage_cols and stage_cols["completed_at"].nullable
    assert "error_message" in stage_cols and stage_cols["error_message"].nullable
    assert "metadata" in stage_cols and not stage_cols["metadata"].nullable
    assert "created_at" in stage_cols and not stage_cols["created_at"].nullable

    stage_constraints = {c.name: c for c in table_constraints["autonomous_deployment_stages"]}
    assert "uq_run_stage" in stage_constraints
    assert "chk_stage_status" in stage_constraints

    # --- Verify autonomous_deployment_logs ---
    log_cols = {c.name: c for c in created_tables["autonomous_deployment_logs"]}
    assert "id" in log_cols and log_cols["id"].primary_key is True
    assert "run_id" in log_cols and not log_cols["run_id"].nullable
    assert "stage_name" in log_cols and not log_cols["stage_name"].nullable
    assert "log_seq" in log_cols and not log_cols["log_seq"].nullable
    assert "level" in log_cols and not log_cols["level"].nullable
    assert "message" in log_cols and not log_cols["message"].nullable
    assert "created_at" in log_cols and not log_cols["created_at"].nullable

    log_constraints = {c.name: c for c in table_constraints["autonomous_deployment_logs"]}
    assert "uq_run_log_seq" in log_constraints
    assert "chk_log_level" in log_constraints

    # --- Verify autonomous_deployment_outbox ---
    outbox_cols = {c.name: c for c in created_tables["autonomous_deployment_outbox"]}
    assert "id" in outbox_cols and outbox_cols["id"].primary_key is True
    assert "run_id" in outbox_cols and not outbox_cols["run_id"].nullable
    assert "event_seq" in outbox_cols and not outbox_cols["event_seq"].nullable
    assert "event_type" in outbox_cols and not outbox_cols["event_type"].nullable
    assert "payload" in outbox_cols and not outbox_cols["payload"].nullable
    assert "status" in outbox_cols and not outbox_cols["status"].nullable
    assert "created_at" in outbox_cols and not outbox_cols["created_at"].nullable

    outbox_constraints = {c.name: c for c in table_constraints["autonomous_deployment_outbox"]}
    assert "uq_run_outbox_seq" in outbox_constraints
    assert "chk_outbox_status" in outbox_constraints

    # Verify created indexes
    idx_names = {idx["name"] for idx in created_indexes}
    assert "idx_auto_deploy_proj_status" in idx_names
    assert "idx_auto_deploy_lease" in idx_names
    assert "idx_auto_deploy_stages_run" in idx_names
    assert "idx_auto_deploy_logs_query" in idx_names
    assert "idx_auto_deploy_outbox_pending" in idx_names

    # 2. Run downgrade
    migration_0038.downgrade()

    # Verify dropped tables in correct cascade order
    assert dropped_tables == [
        "autonomous_deployment_outbox",
        "autonomous_deployment_logs",
        "autonomous_deployment_stages",
        "autonomous_deployments",
    ]
