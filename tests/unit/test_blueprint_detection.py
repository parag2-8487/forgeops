"""Unit tests for Dynamic Project Detection and G1 Blueprint Gate."""

import json
import tempfile
from pathlib import Path
import pytest

from backend.src.blueprint.models import WorkloadType
from backend.src.inspection.blueprint_gate import verify_blueprint_gate
from backend.src.inspection.scanner import scan_repository_tree
from backend.src.inspection.workspace_resolver import resolve_repository_blueprint


def test_single_root_nextjs_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pkg_json = root / "package.json"
        pkg_json.write_text(
            json.dumps({
                "name": "portfolio",
                "scripts": {"build": "next build", "start": "next start"},
                "dependencies": {"next": "14.2.0", "react": "18.2.0"},
            })
        )

        discovered = scan_repository_tree(root)
        assert len(discovered.manifests) == 1
        assert discovered.manifests[0].filename == "package.json"

        bp = resolve_repository_blueprint(discovered)
        assert bp.is_monorepo is False
        assert bp.runtime.framework == "nextjs"
        assert bp.build_config.source_dir == "."
        assert bp.network.listen_port == 3000
        assert bp.workload_type == WorkloadType.WEB_SERVICE

        gate_res = verify_blueprint_gate(bp)
        assert gate_res.passed is True
        assert gate_res.status == "PASSED"


def test_nested_subdirectory_nextjs_detection():
    """Regression test case: Next.js app in nested 'code-review' subdirectory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        sub = root / "code-review"
        sub.mkdir()
        pkg_json = sub / "package.json"
        pkg_json.write_text(
            json.dumps({
                "name": "code-review-app",
                "scripts": {"build": "next build", "start": "next start"},
                "dependencies": {"next": "14.2.0", "react": "18.2.0"},
            })
        )

        discovered = scan_repository_tree(root)
        bp = resolve_repository_blueprint(discovered)

        # Asserts dynamic resolution without path assumptions
        assert bp.build_config.source_dir == "code-review"
        assert bp.runtime.framework == "nextjs"
        assert bp.runtime.start_command == "npm run start"
        assert bp.build_config.build_command == "npm run build"

        gate_res = verify_blueprint_gate(bp)
        assert gate_res.passed is True
        assert gate_res.status == "PASSED"


def test_python_fastapi_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        req_txt = root / "requirements.txt"
        req_txt.write_text("fastapi==0.110.0\nuvicorn==0.28.0\n")

        discovered = scan_repository_tree(root)
        bp = resolve_repository_blueprint(discovered)

        assert bp.runtime.language == "python"
        assert bp.runtime.framework == "fastapi"
        assert bp.network.listen_port == 8000
        assert bp.workload_type == WorkloadType.WEB_SERVICE

        gate_res = verify_blueprint_gate(bp)
        assert gate_res.passed is True


def test_python_worker_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        req_txt = root / "requirements.txt"
        req_txt.write_text("celery==5.3.6\nredis==5.0.0\n")

        discovered = scan_repository_tree(root)
        bp = resolve_repository_blueprint(discovered)

        assert bp.runtime.language == "python"
        assert bp.workload_type == WorkloadType.BACKGROUND_WORKER
        assert bp.network.listen_port is None

        gate_res = verify_blueprint_gate(bp)
        assert gate_res.passed is True


def test_unresolved_ambiguity_gate_behavior():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        app1 = root / "services" / "svc1"
        app1.mkdir(parents=True)
        (app1 / "package.json").write_text(json.dumps({"name": "svc1", "scripts": {"start": "node index.js"}}))

        app2 = root / "services" / "svc2"
        app2.mkdir(parents=True)
        (app2 / "package.json").write_text(json.dumps({"name": "svc2", "scripts": {"start": "node index.js"}}))

        discovered = scan_repository_tree(root)
        bp = resolve_repository_blueprint(discovered)

        assert bp.ambiguity.is_ambiguous is True
        assert len(bp.ambiguity.detected_candidates) == 2

        gate_res = verify_blueprint_gate(bp)
        assert gate_res.passed is False
        assert gate_res.status == "AWAITING_OPERATOR_INPUT"
        assert len(gate_res.candidates) == 2
