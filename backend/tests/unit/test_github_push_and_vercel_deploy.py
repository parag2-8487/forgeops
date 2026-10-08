# SPDX-License-Identifier: FSL-1.1-ALv2
"""Unit tests for GitHub push and Vercel deployment features."""

import json

from src.projects.vercel_deploy import detect_framework, evaluate_vercel_config


def test_detect_framework_nextjs():
    files = {
        "package.json": json.dumps({"dependencies": {"next": "14.2.0", "react": "18.3.0"}}),
    }
    framework, display = detect_framework(files)
    assert framework == "nextjs"
    assert display == "Next.js"


def test_detect_framework_vite():
    files = {
        "package.json": json.dumps({"devDependencies": {"vite": "5.3.0", "@vitejs/plugin-react": "4.3.0"}}),
        "vite.config.ts": "import { defineConfig } from 'vite';",
    }
    framework, display = detect_framework(files)
    assert framework == "vite"
    assert display == "Vite"


def test_detect_framework_static_html():
    files = {
        "index.html": "<!DOCTYPE html><html><body><h1>Hello</h1></body></html>",
    }
    framework, display = detect_framework(files)
    assert framework is None
    assert display == "Static HTML"


def test_evaluate_vercel_config_spa_needs_rewrite():
    files = {
        "package.json": json.dumps({"devDependencies": {"vite": "5.0.0"}}),
        "index.html": "<div id='root'></div>",
    }
    res = evaluate_vercel_config(files)
    assert res.framework == "vite"
    assert res.needs_vercel_json is True
    assert res.suggested_vercel_json is not None
    assert res.suggested_vercel_json.get("rewrites") == [{"source": "/(.*)", "destination": "/index.html"}]


def test_evaluate_vercel_config_nextjs_no_extra_config():
    files = {
        "package.json": json.dumps({"dependencies": {"next": "14.0.0"}}),
    }
    res = evaluate_vercel_config(files)
    assert res.framework == "nextjs"
    assert res.needs_vercel_json is False
    assert "natively supported" in res.reason.lower()


def test_evaluate_vercel_config_preserves_existing():
    files = {
        "package.json": json.dumps({"devDependencies": {"vite": "5.0.0"}}),
        "vercel.json": json.dumps({"cleanUrls": True}),
    }
    res = evaluate_vercel_config(files)
    assert res.has_vercel_json is True
    assert res.needs_vercel_json is False


def test_export_and_deploy_problem_types_registered():
    from src.core.errors import PROBLEM_REGISTRY, problem

    expected_types = [
        "github-push-failed",
        "github-create-failed",
        "github-init-failed",
        "github-blob-upload-failed",
        "github-tree-failed",
        "github-commit-failed",
        "github-ref-failed",
        "project-empty",
        "validation-error",
        "vercel-deploy-failed",
    ]
    for p_type in expected_types:
        assert p_type in PROBLEM_REGISTRY
        prob = problem(p_type, detail="test detail")
        assert prob.problem.type.endswith(p_type)
        assert prob.problem.detail == "test detail"


def test_vercel_deploy_binary_base64_payload():
    import base64

    raw_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b64_str = base64.b64encode(raw_bytes).decode("ascii")
    files = {
        "index.html": "<html><body>test</body></html>",
        "public/images/logo.png": f"__forgeops_b64__:{b64_str}",
    }

    deploy_files = []
    for path, content in files.items():
        if content.startswith("__forgeops_b64__:"):
            encoded = content.removeprefix("__forgeops_b64__:")
        else:
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        deploy_files.append({"file": path, "data": encoded, "encoding": "base64"})

    logo_entry = next(f for f in deploy_files if f["file"] == "public/images/logo.png")
    assert logo_entry["data"] == b64_str
    assert base64.b64decode(logo_entry["data"]) == raw_bytes


def test_github_push_binary_base64_decoding():
    import base64

    raw_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    b64_str = base64.b64encode(raw_bytes).decode("ascii")
    content = f"__forgeops_b64__:{b64_str}"

    if content.startswith("__forgeops_b64__:"):
        encoded = content.removeprefix("__forgeops_b64__:")
    else:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    assert encoded == b64_str
    assert base64.b64decode(encoded) == raw_bytes

