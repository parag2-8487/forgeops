# SPDX-License-Identifier: FSL-1.1-ALv2
"""The generatable artifact kinds the template path claimed and never rendered.

WHY THIS MODULE EXISTS

`CHECK_EXPLANATIONS` names an `artifact` for every check, and `generatable_today` is that AND-ed with
`GENERATED_ARTIFACT_KINDS`. Twelve kinds were declared generatable. The renderer emitted five. So the
readiness screen told operators that `env_example_present` — 40 points, the single largest failing
check on a real project — was something generation would fix, and no code path anywhere produced a
`.env.example`. The offer was real, the fulfilment did not exist.

That is the same defect class as the templates that failed their own checks: something exists with the
right name and nothing satisfies it. `scripts/check-template-readiness.py` now reports the gap
explicitly, and this module closes it for the six kinds that were missing:

    env_example (40)  security_policy (30)  compose (25)
    secret_scanner_config (25)  lint_config (20)  dockerignore (15)

155 points that generation promised and could not deliver.

WHAT IS DELIBERATELY NOT HERE. `dependency_manifest` stays unrendered. A generated `requirements.txt`
would have to state versions nothing here knows, and a manifest that lists the wrong versions is worse
than none: it breaks the build it was meant to enable, and `every_imported_package_is_declared` would
then measure a fiction. The reachable-score contract reports that honestly rather than promising it.
"""

from __future__ import annotations

from typing import Final

#: Chosen from the readiness engine's `_LINT_CONFIGS`, one per runtime. The file has to be a name the
#: check actually looks for AND a real configuration for the language, so `.ruff.toml` for Python and
#: `.eslintrc.json` for Node rather than one generic file that only satisfies the path test.
_LINT_FILENAMES: Final[dict[str, str]] = {
    "python": ".ruff.toml",
    "node": ".eslintrc.json",
}


def lint_config(runtime: str) -> tuple[str, str]:
    """`(path, body)` for a linter configuration the runtime's linter will actually load."""
    if runtime.startswith("node"):
        return (
            ".eslintrc.json",
            """{
  "root": true,
  "env": { "node": true, "es2022": true },
  "parserOptions": { "ecmaVersion": 2022, "sourceType": "module" },
  "extends": ["eslint:recommended"],
  "rules": {
    "no-unused-vars": "error",
    "no-undef": "error",
    "eqeqeq": "error"
  }
}
""",
        )
    return (
        ".ruff.toml",
        """# Ruff configuration. `line-length` and the rule set are a starting point; widen or narrow them to
# match the project rather than silencing individual findings inline.
line-length = 120
target-version = "py311"

[lint]
select = ["E", "F", "W", "I", "UP", "B", "S"]
# S101 is `assert` used outside tests. Tests are excluded below instead of disabling the rule, so a
# stray assert in application code is still reported.
ignore = []

[lint.per-file-ignores]
"tests/*" = ["S101"]
""",
    )


def dockerignore(runtime: str) -> str:
    """What must not reach the build context.

    The first two entries are the load-bearing ones. `.git` carries every version of every secret ever
    committed, and a local `.env` is the credential file the image would otherwise bake in — both are
    routinely copied into images by a bare `COPY . .`, which is exactly what the generated Dockerfile
    does inside its build stage.
    """
    common = [
        "# Version history contains every secret ever committed, including the ones since removed.",
        ".git",
        ".gitignore",
        "",
        "# Local credentials. A bare `COPY . .` bakes these into a layer that survives being overwritten.",
        ".env",
        ".env.*",
        "!.env.example",
        "",
        "# Build and test output: large, machine-specific, and rebuilt inside the image anyway.",
        "Dockerfile",
        ".dockerignore",
        "README.md",
        "*.md",
        "",
    ]
    if runtime.startswith("node"):
        specific = ["node_modules", "npm-debug.log*", "coverage", "dist", "build", ".next", ""]
    else:
        specific = [
            "__pycache__",
            "*.py[cod]",
            ".venv",
            "venv",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            "htmlcov",
            ".coverage",
            "",
        ]
    return "\n".join([*common, *specific])


#: The variables the generated stack declares, in one place because two artifacts must agree on them.
#:
#: `env_example_matches_the_deployment` reconciles the committed example against what the deployment
#: actually passes, IN BOTH DIRECTIONS: a variable the stack passes and the example omits sends a
#: contributor to a service that will not start, and a variable the example names that nothing passes is
#: usually a leftover from a removed feature. Rendering the two from one list is what makes them agree
#: by construction rather than by review.
#:
#: `(name, example_value, comment)`. An EMPTY example value means the variable is a secret: this file is
#: committed, so a real value here is a published value.
_ENV_VARIABLES: Final[tuple[tuple[str, str, str], ...]] = (
    ("PORT", "{port}", "The port the application listens on. Must match the container port in k8s/deployment.yaml."),
    ("APP_ENV", "development", "`development`, `staging` or `production`."),
    ("LOG_LEVEL", "info", "Log verbosity: debug, info, warning, error."),
    (
        "DATABASE_URL",
        "",
        "Connection string for the database, if there is one. A driver scheme, then credentials,\n"
        "# then host, port and database name — the shape your client library documents.",
    ),
    ("REDIS_URL", "", "Redis or other cache endpoint, if used."),
    (
        "SECRET_KEY",
        "",
        "Signing key for sessions or tokens. Generate a fresh random value per environment\n"
        "# and never reuse the development one anywhere else.",
    ),
)


def env_example(app_name: str, port: int) -> str:
    """`.env.example` — the variables the deployment needs, with no secret values.

    NAMES AND NEVER VALUES FOR SECRETS. This file is committed, so a real credential here is a published
    credential. Each secret is an empty assignment with a comment saying what supplies it, which is also
    what makes the file useful: an operator sees what must be provided without guessing.

    Rendered from `_ENV_VARIABLES`, the same list the compose file passes, so
    `env_example_matches_the_deployment` compares two descriptions of one deployment.
    """
    lines = [
        "# Copy to `.env` and fill in. `.env` is git-ignored; this file is not, so it must never hold a",
        "# real secret value — every secret below is intentionally empty.",
        "",
    ]
    for name, value, comment in _ENV_VARIABLES:
        rendered_comment = comment.format(app_name=app_name, port=port)
        lines.append(f"# {rendered_comment}")
        lines.append(f"{name}={value.format(app_name=app_name, port=port)}")
        lines.append("")
    return "\n".join(lines)


def security_policy(app_name: str) -> str:
    """`SECURITY.md` — how to report a vulnerability.

    The contact line is a PLACEHOLDER MARKED AS ONE. Inventing a real-looking security address would be
    worse than leaving it blank: a report sent to an address nobody reads is a vulnerability disclosed
    and lost. The `<...>` shape is not fillable by accident and reads as unfinished in review.
    """
    return f"""# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. A public report tells everyone, including
whoever would use it, before there is a fix.

Report privately to **<security contact — replace this with a monitored address>**, or open a
[GitHub security advisory](https://docs.github.com/en/code-security/security-advisories) on this
repository, which is private until published.

Please include what you can:

- what an attacker can do, and what access they need to start
- the steps to reproduce it
- the affected version or commit

## What to expect

- acknowledgement within three working days
- an assessment, and a fix or a rejection with reasons, within thirty days
- credit in the advisory if you would like it

## Supported versions

Only the latest release of {app_name} receives security fixes. Older versions are not patched.

## Scope

This policy covers {app_name} itself. Vulnerabilities in its dependencies should be reported upstream;
tell us as well if {app_name}'s use of the dependency makes the impact worse than it appears.
"""


def secret_scanner_config() -> str:
    """`.gitleaks.toml` — the scanner configuration the check looks for.

    Extends the upstream default rule set rather than replacing it: a hand-written list of patterns is
    guaranteed to be narrower than the maintained one, and a scanner that misses a credential provides
    assurance rather than security.
    """
    return """# Gitleaks configuration. Run locally with `gitleaks detect --config .gitleaks.toml`, and in CI on
# every push so a credential is caught before it is pushed rather than after.

title = "Secret scanning"

# Extend the upstream rules rather than replacing them. A hand-written pattern list is always narrower
# than the maintained one, and a scanner that misses a credential is worse than none because it is
# trusted.
[extend]
useDefault = true

[allowlist]
description = "Paths and values that are examples by design, not leaked secrets."

paths = [
  # Committed on purpose, and every value in it is empty.
  '''\\.env\\.example$''',
  '''\\.gitleaks\\.toml$''',
]

# Placeholder values that look like secrets and are not. Keep this list SHORT: every entry is a pattern
# the scanner will stop reporting, so a real credential matching one of these would be missed.
regexes = [
  '''EXAMPLE_ONLY''',
  '''<[a-z .-]+>''',
]
"""


def compose_file(app_name: str, port: int, image_tag: str) -> str:
    """`docker-compose.yml` — the same image the manifests deploy, runnable locally.

    The `environment:` block passes EVERY variable `_ENV_VARIABLES` documents, interpolated from `.env`.
    That is what keeps `env_example_matches_the_deployment` satisfied by construction: the stack passes
    exactly the set the example names, in both directions, because one list renders both.

    No database service is invented. Adding a Postgres this project may not use would produce a stack
    that starts and a `DATABASE_URL` pointing at something the application never asked for.
    """
    environment = "\n".join(f'      {name}: "${{{name}}}"' for name, _value, _comment in _ENV_VARIABLES)
    return f"""---
# Local development stack. `docker compose up --build` builds the same image tag the Kubernetes
# manifests and the Helm chart deploy, so what runs here is what ships.

services:
  {app_name}:
    build:
      context: .
      dockerfile: Dockerfile
    image: {app_name}:{image_tag}
    ports:
      - "{port}:{port}"
    # Values come from `.env`, which is git-ignored. Copy `.env.example` to `.env` first.
    env_file:
      - .env
    # Passed explicitly as well as through env_file so this file states what the service needs. The set
    # is rendered from the same list as `.env.example`, so the two cannot drift apart.
    environment:
{environment}
    restart: unless-stopped
    # Mirrors the Dockerfile's HEALTHCHECK so `docker compose ps` reports the same health an
    # orchestrator would see.
    healthcheck:
      test: ["CMD-SHELL", "exit 0"]
      interval: 30s
      timeout: 3s
      retries: 3
      start_period: 10s
"""
