"""Unit tests for Level 1, Level 2 Artifact Validation and G3 Consistency Gate."""

import tempfile
from pathlib import Path
import pytest

from backend.src.blueprint.models import BuildConfig, NetworkContract, ProjectBlueprint, RuntimeContract, WorkloadType
from backend.src.validation.artifact_gate import evaluate_existing_artifact_gate
from backend.src.validation.blueprint_compatibility import (
    validate_dockerfile_blueprint_compatibility,
)
from backend.src.validation.consistency_gate import verify_consistency_gate
from backend.src.validation.syntax_validator import (
    validate_compose_schema,
    validate_dockerfile_syntax,
)


def create_sample_blueprint(source_dir: str = ".", port: int = 3000, lang: str = "nodejs") -> ProjectBlueprint:
    return ProjectBlueprint(
        blueprint_id="test-bp",
        repository_root="",
        workload_type=WorkloadType.WEB_SERVICE,
        build_config=BuildConfig(source_dir=source_dir),
        runtime=RuntimeContract(language=lang, runtime_version="20", start_command="npm run start"),
        network=NetworkContract(listen_port=port),
    )


def test_level1_dockerfile_valid_and_invalid():
    valid_df = "FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nEXPOSE 3000\nCMD [\"npm\", \"start\"]\n"
    res = validate_dockerfile_syntax(valid_df)
    assert res.is_valid is True
    assert 3000 in res.parsed_metadata["exposed_ports"]

    invalid_df = "WORKDIR /app\nCOPY . .\n"
    res2 = validate_dockerfile_syntax(invalid_df)
    assert res2.is_valid is False
    assert any("FROM" in e for e in res2.errors)


def test_level2_compatibility_port_and_language():
    bp = create_sample_blueprint(port=3000, lang="nodejs")
    df_wrong_lang = "FROM python:3.11\nWORKDIR /app\nEXPOSE 3000\nCMD [\"python\", \"main.py\"]\n"
    comp_res = validate_dockerfile_blueprint_compatibility(df_wrong_lang, bp)
    assert comp_res.is_compatible is False
    assert any("Base image" in r for r in comp_res.reasons)

    df_wrong_port = "FROM node:20-alpine\nWORKDIR /app\nEXPOSE 8080\nCMD [\"npm\", \"start\"]\n"
    comp_res2 = validate_dockerfile_blueprint_compatibility(df_wrong_port, bp)
    assert comp_res2.is_compatible is False
    assert any("Exposed port" in r for r in comp_res2.reasons)


def test_g3_consistency_gate_catches_missing_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        df = root / "Dockerfile"
        # Dockerfile references non_existent.txt
        df.write_text("FROM node:20\nWORKDIR /app\nCOPY non_existent.txt /app/\nEXPOSE 3000\nCMD [\"npm\", \"start\"]\n")

        bp = create_sample_blueprint()
        bp.repository_root = str(root)

        gate_res = verify_consistency_gate(root, df, None, bp)
        assert gate_res.passed is False
        assert any("non_existent.txt" in e for e in gate_res.errors)
