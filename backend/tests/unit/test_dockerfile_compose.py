# SPDX-License-Identifier: FSL-1.1-ALv2
"""Static assertions for Dockerfile and docker-compose.yml (task 5.7).

Docker is not installed, so we verify via file content inspection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
COMPOSE_PATH = ROOT / "docker-compose.yml"


class TestDockerfile:
    """Static assertions about the backend Dockerfile."""

    def _read_dockerfile(self) -> str:
        return (BACKEND / "Dockerfile").read_text(encoding="utf-8")

    def test_multi_stage_build(self) -> None:
        """Dockerfile has builder and runtime stages."""
        content = self._read_dockerfile()
        assert "AS builder" in content
        assert "AS runtime" in content

    def test_installs_only_requirements_lock(self) -> None:
        """Only requirements.lock is installed, not requirements-dev.lock."""
        content = self._read_dockerfile()
        assert "requirements.lock" in content
        assert "--require-hashes" in content

    def test_dev_lock_absent_from_runtime(self) -> None:
        """requirements-dev.lock is never COPY'd into the image."""
        content = self._read_dockerfile()
        # Check that requirements-dev.lock is not in any COPY instruction
        for line in content.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("COPY") and "requirements-dev" in line_stripped:
                pytest.fail("requirements-dev.lock should not be in a COPY instruction")

    def test_runs_as_non_root(self) -> None:
        """Dockerfile has a USER directive (non-root)."""
        content = self._read_dockerfile()
        assert "USER" in content
        # Should not be USER root
        for line in content.splitlines():
            if line.strip().startswith("USER"):
                assert "root" not in line.strip().lower()

    def test_exposes_runtime_target(self) -> None:
        """There is a 'runtime' target stage."""
        content = self._read_dockerfile()
        assert "AS runtime" in content

    def test_exposes_port_8000(self) -> None:
        """EXPOSE 8000 is present."""
        content = self._read_dockerfile()
        assert "EXPOSE 8000" in content


class TestComposeBackendService:
    """Static assertions about the backend service in docker-compose.yml."""

    @pytest.fixture(autouse=True)
    def _load_compose(self) -> None:
        with open(COMPOSE_PATH, encoding="utf-8") as f:
            self.compose = yaml.safe_load(f)

    def test_backend_service_exists(self) -> None:
        """backend service is defined."""
        assert "backend" in self.compose["services"]

    def test_backend_build_target_runtime(self) -> None:
        """backend build target is 'runtime'."""
        svc = self.compose["services"]["backend"]
        assert svc["build"]["target"] == "runtime"

    def test_backend_depends_on_postgres_redis_opa(self) -> None:
        """backend depends_on postgres, redis, opa with service_started."""
        svc = self.compose["services"]["backend"]
        deps = svc["depends_on"]
        assert deps["postgres"]["condition"] == "service_started"
        assert deps["redis"]["condition"] == "service_started"
        assert deps["opa"]["condition"] == "service_started"

    def test_backend_healthcheck_uses_health(self) -> None:
        """backend healthcheck hits /health (liveness only)."""
        svc = self.compose["services"]["backend"]
        hc = svc["healthcheck"]
        test_str = " ".join(hc["test"]) if isinstance(hc["test"], list) else hc["test"]
        assert "/health" in test_str
        # Must NOT check /health/ready (that's readiness, not liveness)
        assert "/health/ready" not in test_str

    def test_backend_port_loopback(self) -> None:
        """backend port binding is loopback only."""
        svc = self.compose["services"]["backend"]
        for port in svc["ports"]:
            assert port.startswith("127.0.0.1:") or "${" in port

    def test_env_file_uses_anchor(self) -> None:
        """backend uses env_file with the anchor semantics."""
        svc = self.compose["services"]["backend"]
        assert "env_file" in svc

    def test_no_optional_profile_services(self) -> None:
        """Optional-profile services are present but ONLY under their profiles.

        The default-profile set is read from `scripts/compose-default-services.txt`, the
        same source `scripts/check-compose-validate.py` uses. It was previously asserted
        as the literal count 5, which was a third copy of the same data: promoting a
        service in the data file left this test failing for a reason that had nothing to
        do with what it checks. A count also proved less than it appeared to — five wrong
        services would have satisfied it.
        """
        root = Path(__file__).resolve().parents[3]
        listed = {
            line.strip()
            for line in (root / "scripts" / "compose-default-services.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert listed, "the default-service list is empty; an empty expectation proves nothing"

        services = self.compose["services"]
        default_services = {name for name, cfg in services.items() if "profiles" not in cfg}
        assert default_services == listed, (
            f"unprofiled services {sorted(default_services)} do not match the committed "
            f"default set {sorted(listed)}"
        )

        # Profiled services must have their profiles set correctly
        if "infisical" in services:
            assert "profiles" in services["infisical"]
            assert "vault" in services["infisical"]["profiles"]
        if "agent-dev" in services:
            assert "profiles" in services["agent-dev"]
            assert "tools" in services["agent-dev"]["profiles"]

    def test_existing_services_preserved(self) -> None:
        """postgres, redis, opa still exist."""
        services = self.compose["services"]
        assert "postgres" in services
        assert "redis" in services
        assert "opa" in services

    def test_liveness_healthcheck_no_dep_io(self) -> None:
        """The healthcheck only hits /health which does no dependency I/O.
        This means dependency outage cannot fail the liveness healthcheck."""
        svc = self.compose["services"]["backend"]
        hc_test = " ".join(svc["healthcheck"]["test"])
        # /health is liveness, no DB/Redis checks
        assert "/health'" in hc_test or '/health"' in hc_test or "health" in hc_test
        assert "ready" not in hc_test
