# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.generation.dry_run import DryRunStage

pytestmark = [pytest.mark.mandatory]

def test_dry_run_dockerfile_valid():
    stage = DryRunStage()
    res = stage.validate_dockerfile("FROM python:3.11\nCMD ['python']")
    assert res.valid is True
    assert len(res.errors) == 0

def test_dry_run_dockerfile_invalid():
    stage = DryRunStage()
    res = stage.validate_dockerfile("RUN echo hello")
    assert res.valid is False
    assert "Missing FROM directive" in res.errors[0]

def test_dry_run_k8s_manifest_valid():
    stage = DryRunStage()
    res = stage.validate_k8s_manifest("apiVersion: apps/v1\nkind: Deployment")
    assert res.valid is True
