# SPDX-License-Identifier: FSL-1.1-ALv2
"""The fallback Dockerfile serves a static frontend instead of running a `start` script it does not have."""

from __future__ import annotations

from src.generation.artifact_checks import validate_artifacts
from src.generation.service import GenerationService, _static_frontend_output_dir

VITE_PKG = '{"scripts": {"dev": "vite", "build": "tsc -b && vite build"}, "devDependencies": {"vite": "^8"}}'
INV = {"languages": ["typescript"], "frameworks": [{"name": "Vite"}, {"name": "React"}], "manifests": ["package.json"]}


def _dockerfile(existing: dict[str, str]) -> str:
    files = GenerationService()._render(
        "x", project={"name": "site", "settings": {}, "inventory": INV}, existing=existing
    )
    return next(f.content for f in files if f.path == "Dockerfile")


def test_output_dir_comes_from_the_project() -> None:
    kw = {"runtime": "node", "is_frontend": True, "is_nextjs": False, "frameworks": []}
    assert _static_frontend_output_dir(existing={"package.json": VITE_PKG}, **kw) == "dist"
    assert (
        _static_frontend_output_dir(
            existing={
                "package.json": VITE_PKG,
                "vite.config.ts": "export default { build: { outDir: 'public_html' } }",
            },
            **kw,
        )
        == "public_html"
    )
    cra = '{"scripts": {"build": "react-scripts build"}, "dependencies": {"react-scripts": "5"}}'
    assert _static_frontend_output_dir(existing={"package.json": cra}, **kw) == "build"


def test_a_project_with_its_own_server_is_not_treated_as_static() -> None:
    kw = {"runtime": "node", "is_frontend": True, "is_nextjs": False, "frameworks": []}
    with_start = '{"scripts": {"build": "vite build", "start": "node server.js"}}'
    assert _static_frontend_output_dir(existing={"package.json": with_start}, **kw) is None
    assert _static_frontend_output_dir(existing={"package.json": VITE_PKG}, **{**kw, "is_nextjs": True}) is None


def test_the_static_fallback_passes_the_gate_and_never_runs_npm_start() -> None:
    existing = {"package.json": VITE_PKG, "package-lock.json": "{}", "index.html": "", "src/main.tsx": ""}
    dockerfile = _dockerfile(existing)
    assert "npm start" not in dockerfile and 'CMD ["npm", "start"]' not in dockerfile
    assert "COPY --from=builder /app/dist /usr/share/nginx/html" in dockerfile
    printf_line = next(line for line in dockerfile.splitlines() if "printf" in line)
    assert "listen 3000" in printf_line and "try_files" in printf_line, "the nginx config must be one RUN line"
    files = GenerationService()._render(
        "x", project={"name": "site", "settings": {}, "inventory": INV}, existing=existing
    )
    assert [f for f in validate_artifacts(list(files), existing=existing) if f.startswith("Dockerfile")] == []
