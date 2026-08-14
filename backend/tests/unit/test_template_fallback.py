# SPDX-License-Identifier: FSL-1.1-ALv2
import pytest
from src.generation.template_fallback import TemplateFallback

pytestmark = [pytest.mark.mandatory]


def test_dockerfile_template_fallback():
    tmpl = TemplateFallback.get_dockerfile_template("3.11-slim")
    assert "FROM python:3.11-slim" in tmpl
    assert "Terminal Cascade Template Fallback" in tmpl


def test_k8s_manifest_template_fallback():
    tmpl = TemplateFallback.get_k8s_manifest_template("devops-svc")
    assert "name: devops-svc" in tmpl
    assert "kind: Deployment" in tmpl
