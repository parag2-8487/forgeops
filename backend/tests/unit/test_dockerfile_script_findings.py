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
    dockerfile = "FROM node:22\nCOPY package*.json ./\nRUN npm install --production\nRUN npm run build\n"
    findings = dockerfile_script_findings(dockerfile, VITE)
    assert len(findings) == 1 and "devDependencies typescript, vite" in findings[0]


def test_a_new_stage_resets_the_production_install() -> None:
    dockerfile = (
        "FROM node:22 AS deps\nRUN npm ci --omit=dev\n"
        "FROM node:22 AS build\nCOPY package*.json ./\nRUN npm ci\nRUN npm run build\n"
    )
    assert dockerfile_script_findings(dockerfile, VITE) == []


def test_correct_dockerfiles_pass_for_every_package_manager() -> None:
    for dockerfile in (
        "FROM node:22\nCOPY package*.json ./\nRUN npm ci\nRUN npm run build\n",
        "FROM node:22\nCOPY . .\nRUN yarn install --frozen-lockfile\nRUN yarn build\n",
        "FROM node:22\nCOPY . .\nRUN pnpm install\nRUN pnpm run preview\n",
    ):
        assert dockerfile_script_findings(dockerfile, VITE) == [], dockerfile


def test_workspace_package_scripts_count() -> None:
    existing = {"package.json": '{"workspaces": ["web"]}', "web/package.json": '{"scripts": {"start": "next start"}}'}
    assert dockerfile_script_findings('FROM node:22\nCOPY . .\nCMD ["npm", "start"]\n', existing) == []


def test_projects_without_package_json_are_not_judged() -> None:
    assert dockerfile_script_findings('FROM node:22\nCMD ["npm", "start"]\n', {"requirements.txt": "flask"}) == []


def test_the_gate_reports_it_against_the_dockerfile() -> None:
    findings = validate_artifacts(
        [_Artifact("Dockerfile", 'FROM node:22\nUSER node\nCMD ["npm", "start"]\n')], existing=VITE
    )
    assert any(f.startswith("Dockerfile: ") and "'start'" in f for f in findings)


def test_compose_may_not_build_from_a_dockerfile_that_will_not_exist() -> None:
    from src.generation.artifact_checks import compose_missing_dockerfiles

    compose = _Artifact(
        "docker-compose.yml", "services:\n  app:\n    build:\n      context: .\n      dockerfile: Dockerfile\n"
    )
    assert compose_missing_dockerfiles([compose], {"package.json": "{}"}), "a withheld Dockerfile must fail the set"
    assert compose_missing_dockerfiles([compose, _Artifact("Dockerfile", "FROM node:22\n")], {}) == []
    assert compose_missing_dockerfiles([compose], {"Dockerfile": "FROM node:22\n"}) == []
    nested = _Artifact("docker-compose.yml", "services:\n  web:\n    build: ./web\n")
    assert compose_missing_dockerfiles([nested], {"web/Dockerfile": "x"}) == []
    assert "web/Dockerfile" in compose_missing_dockerfiles([nested], {})[0]


def test_npm_install_prod_is_a_production_install_and_tsc_needs_typescript() -> None:
    vite = {
        "package.json": '{"scripts": {"build": "tsc -b && vite build"}, "devDependencies": {"typescript": "~6", "vite": "^8"}}'
    }
    dockerfile = "FROM node:22\nWORKDIR /app\nCOPY package.json ./\nRUN npm install --prod\nRUN npm run build\n"
    findings = dockerfile_script_findings(dockerfile, vite)
    assert len(findings) == 1 and "typescript, vite" in findings[0]


def test_a_final_stage_with_only_build_output_cannot_run_a_script() -> None:
    dockerfile = (
        "FROM node:22 AS build\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\nCOPY . .\nRUN npm run build\n"
        'FROM node:22\nWORKDIR /app\nCOPY --from=build /app/dist ./dist\nCMD ["npm", "run", "preview"]\n'
    )
    findings = dockerfile_script_findings(dockerfile, VITE)
    assert any("never copies package.json" in f for f in findings)


def test_copying_the_whole_build_stage_brings_manifest_and_modules() -> None:
    dockerfile = (
        "FROM node:22 AS build\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\nCOPY . .\nRUN npm run build\n"
        'FROM node:22\nWORKDIR /app\nCOPY --from=build /app ./\nCMD ["npm", "run", "preview"]\n'
    )
    assert dockerfile_script_findings(dockerfile, VITE) == []


def test_copy_sources_must_exist_in_the_project() -> None:
    from src.generation.artifact_checks import dockerfile_missing_copy_sources

    existing = {"package.json": "{}", "package-lock.json": "{}", "src/main.tsx": "", "public/a.png": ""}
    bad = "FROM node:22\nCOPY package.json yarn.lock ./\nCOPY src ./src\nCOPY --from=x /out ./out\n"
    findings = dockerfile_missing_copy_sources(bad, existing)
    assert len(findings) == 1 and "'yarn.lock'" in findings[0]
    good = 'FROM node:22\nCOPY package*.json ./\nCOPY ["src", "./src"]\nCOPY public/ ./public/\nCOPY . .\n'
    assert dockerfile_missing_copy_sources(good, existing) == []


def test_compose_services_may_not_share_a_host_port_or_publish_the_wrong_one() -> None:
    from src.generation.artifact_checks import validate_compose

    clash = (
        "services:\n"
        '  db:\n    image: postgres:16-alpine\n    ports: ["9002:9002"]\n'
        '  web:\n    build: .\n    ports: ["9002:9002"]\n    environment:\n      PORT: "3000"\n'
    )
    findings = validate_compose(clash)
    assert any("both publish host port 9002" in f for f in findings)
    assert any("sets PORT=3000" in f for f in findings)
    good = (
        "services:\n"
        '  db:\n    image: postgres:16-alpine\n    ports: ["5432:5432"]\n'
        '  web:\n    build: .\n    ports: ["9002:3000"]\n    environment: ["PORT=3000"]\n'
        '  admin:\n    build: ./admin\n    ports: ["127.0.0.1:9003:3000"]\n'
    )
    assert validate_compose(good) == []
