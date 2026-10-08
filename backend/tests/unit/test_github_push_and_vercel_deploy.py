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
