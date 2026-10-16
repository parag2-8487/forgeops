# SPDX-License-Identifier: FSL-1.1-ALv2
"""Wiring for the collaborators Phase 2 composed. Phase 2 2.10, 2.11, 2.13.

`test_wiring_coverage` refuses a collaborator that `create_app()` composes and no test drives through real
wiring, and it named these five. The demand is right: a composed object nobody exercises through the app is
the recurring defect of this codebase in its purest form -- `DeploymentService.complete` had a production
caller only in the sense that something constructed it.

Each assertion below reads the object off `app.state` and exercises the behaviour that makes composition
matter, rather than merely asserting the attribute exists. An `isinstance` check would pass on an object wired
to nothing.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI

from .wiring import wires


@wires("telemetry", "metrics_reader", "metrics_evidence")
class TestTheObservabilityTierIsComposed:
    """2.10's three: the writer, the reader, and the incident-evidence adapter over the reader."""

    def test_telemetry_is_composed_and_reports_whether_it_is_configured(self, production_app: FastAPI) -> None:
        """A no-op when unconfigured, and `enabled` says so -- which is what lets readiness distinguish
        "no collector" from "collector unreachable"."""
        telemetry = production_app.state.telemetry
        assert telemetry is not None
        # Unconfigured in the test app, and that is the supported state rather than a failure.
        assert telemetry.enabled is False

    def test_the_metrics_reader_answers_unconfigured_rather_than_raising(self, production_app: FastAPI) -> None:
        reader = production_app.state.metrics_reader
        assert reader is not None
        assert reader.configured is False

    @pytest.mark.asyncio
    async def test_the_evidence_adapter_reads_through_the_shared_catalogue(self, production_app: FastAPI) -> None:
        """The seam 2.11 depends on: it must produce evidence records, and with no store configured every
        one must be `reachable=False` with prose -- never silently empty, which the evidence floor would
        then read as "nothing to check" rather than "nothing readable"."""
        evidence = production_app.state.metrics_evidence
        records = await evidence.incident_evidence(tenant_id=None)
        assert records, "the adapter produced no evidence records at all"
        assert all(record.reachable is False for record in records)
        assert all(len(record.summary) > 30 for record in records)
        # And each names why, rather than just failing.
        assert any("No metrics store is configured" in record.summary for record in records)


@wires("deployment_incidents")
class TestTheDeploymentIncidentRecorderIsComposed:
    def test_the_deployment_service_holds_the_recorder(self, production_app: FastAPI) -> None:
        """Composed INTO the deployment service, not merely beside it. This is the wiring that makes
        `DeploymentService.fail` file an incident, and the whole point of 2.11's ingestion having a real
        production caller."""
        recorder = production_app.state.deployment_incidents
        assert recorder is not None
        service = production_app.state.deployment_service
        assert service._incidents is recorder  # noqa: SLF001 - the wiring IS the assertion


@wires("memory_port")
class TestTheLearningMemoryPortIsComposed:
    @pytest.mark.asyncio
    async def test_it_reports_an_empty_memory_for_an_unknown_project(self, production_app: FastAPI, conn) -> None:
        """Exercised rather than type-checked. An empty memory for a project with no preferences is the
        honest answer, and `is_empty` is what prompt assembly branches on -- so a port wired to nothing
        would look identical until a preference existed."""
        port = production_app.state.memory_port
        memory = await port.memory_for_prompt(conn, project_id=uuid.uuid4())
        assert memory.is_empty is True
        assert memory.included == []
        assert memory.excluded == []
