# SPDX-License-Identifier: FSL-1.1-ALv2
"""A generated Dockerfile may only run scripts the project declares, with the dependencies they need.

Both rules come from one generated Dockerfile that built and then crash-looped on a Vite project
(2026-11-02): `CMD ["npm", "start"]` with no `start` script, and `npm install --production` before a
build whose tools are devDependencies.
"""

from __future__ import annotations

from src.generation.artifact_checks import dockerfile_script_findings, validate_artifacts

VITE = {
    "package.json": (
        '{"scripts": {"dev": "vite", "build": "tsc -b && vite build", "preview": "vite preview"},'
        ' "devDependencies": {"vite": "^8", "typescript": "~6"}}'
    )
}


class _Artifact:
    def __init__(self, path: str, content: str) -> None:
        self.path = path
        self.content = content


def test_a_script_the_project_does_not_declare_is_refused() -> None:
    findings = dockerfile_script_findings('FROM node:22\nCMD ["npm", "start"]\n', VITE)
    assert len(findings) == 1 and "'start'" in findings[0] and "build, dev, preview" in findings[0]


def test_a_production_install_before_a_dev_tool_build_is_refused() -> None:
    findings = dockerfile_script_findings("FROM node:22\nRUN npm install --production\nRUN npm run build\n", VITE)
    assert len(findings) == 1 and "devDependencies vite" in findings[0]


def test_a_new_stage_resets_the_production_install() -> None:
    dockerfile = "FROM node:22 AS deps\nRUN npm ci --omit=dev\nFROM node:22 AS build\nRUN npm ci\nRUN npm run build\n"
    assert dockerfile_script_findings(dockerfile, VITE) == []


def test_correct_dockerfiles_pass_for_every_package_manager() -> None:
    for dockerfile in (
        "FROM node:22\nRUN npm ci\nRUN npm run build\n",
        "FROM node:22\nRUN yarn install --frozen-lockfile\nRUN yarn build\n",
        "FROM node:22\nRUN pnpm install\nRUN pnpm run preview\n",
    ):
        assert dockerfile_script_findings(dockerfile, VITE) == [], dockerfile


def test_workspace_package_scripts_count() -> None:
    existing = {"package.json": '{"workspaces": ["web"]}', "web/package.json": '{"scripts": {"start": "next start"}}'}
    assert dockerfile_script_findings('FROM node:22\nCMD ["npm", "start"]\n', existing) == []


def test_projects_without_package_json_are_not_judged() -> None:
    assert dockerfile_script_findings('FROM node:22\nCMD ["npm", "start"]\n', {"requirements.txt": "flask"}) == []


def test_the_gate_reports_it_against_the_dockerfile() -> None:
    findings = validate_artifacts(
        [_Artifact("Dockerfile", 'FROM node:22\nUSER node\nCMD ["npm", "start"]\n')], existing=VITE
    )
    assert any(f.startswith("Dockerfile: ") and "'start'" in f for f in findings)
