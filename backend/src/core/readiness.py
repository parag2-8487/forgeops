# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deterministic deployment-readiness scoring engine (phases.md §1.4, Leaf 12.3).

**What changed and why.** The engine used to score a `project_data` dict that its only
caller built from `projects.settings` — the JSONB blob an operator types into the create
form. `config_files` was literally `sorted(settings.keys())`, so a project scored points
for *having been configured*, and `"README.md"` was substituted when the blob was empty.
The number that reached the readiness screen therefore described what somebody had typed,
not what was in the repository, and it moved when the settings changed and stayed still
when the repository did.

It now scores the INDEX: the `file_tree` paths and `file_contents` bodies a real agent scan
persisted. `projects.settings` may still REFINE the evidence — `ignore_globs` removes paths
from consideration, because a path the operator has declared out of scope is not evidence
about the deployment — but it can no longer substitute for it. A project with no scan
scores zero and says so, which is the honest answer: the alternative, scoring the settings,
is a number that looks like a measurement.

**The category set is phases.md §1.4's**: Containerization, CI/CD, Orchestration, Env
Config, Security, IaC. §1.4 names them but fixes no weights, so the weights below are a
decision recorded here rather than a quotation; they sum to 100 so the overall score is a
weighted mean and not an arbitrary sum that happens to land near 100.

**`has_tests` no longer defaults to true.** It defaulted to `True`, which handed every
project 25 of its 100 points for tests nobody had looked for — the single largest source of
inflation in the old score. Test evidence is now a check inside CI/CD, derived from paths
that are actually test files, and an unstated `has_tests` is derived rather than assumed.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

from pydantic import BaseModel, Field

from .index_evidence import IndexEvidence
from .manifest_facts import (
    dockerfile_base_pinned,
    dockerfile_healthcheck,
    kubernetes_image_tags_pinned,
    kubernetes_probes,
    kubernetes_resource_limits,
    pipeline_actions_pinned,
    pipeline_runs_tests,
    pipeline_stages_declared,
)

#: The §1.4 categories and their weights. Summing to 100 makes the overall score a
#: weighted mean of six 0-100 category scores, which is what lets one category's absence
#: be read off the breakdown instead of inferred from a total.
CATEGORY_WEIGHTS: Final[dict[str, int]] = {
    "containerization": 25,
    "ci_config": 20,
    "orchestration": 15,
    "env_config": 15,
    "security_policy": 15,
    "iac": 10,
}

#: Category name → the breakdown field it is reported under. Three of the six keep the
#: names the previous five-category response used (`containerization_score`,
#: `ci_config_score`, `security_policy_score`) so an existing client reading those
#: continues to read the same concept.
CATEGORY_FIELDS: Final[dict[str, str]] = {
    "containerization": "containerization_score",
    "ci_config": "ci_config_score",
    "orchestration": "orchestration_score",
    "env_config": "env_config_score",
    "security_policy": "security_policy_score",
    "iac": "iac_score",
}

#: Paths that are test evidence. Matched on the whole slash-separated path, so
#: `internal/scanner/scanreport_test.go` counts and `contest.py` does not.
_TEST_PATTERNS: Final[tuple[str, ...]] = (
    "*_test.go",
    "test_*.py",
    "*_test.py",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
    "*_spec.rb",
    "*Test.java",
    "*Tests.cs",
)

#: Directory names whose presence is test evidence on its own.
_TEST_DIRECTORIES: Final[tuple[str, ...]] = ("tests", "test", "__tests__", "spec", "e2e")

_LOCKFILES: Final[tuple[str, ...]] = (
    "requirements.lock",
    "requirements-dev.lock",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.sum",
    "cargo.lock",
    "gemfile.lock",
    "composer.lock",
)

#: Files whose presence in a repository is committed key material. Absence scores; it is
#: the one check where the honest signal is a negative, and stating it that way is
#: deliberate — "no private keys in the tree" is a fact about the tree, not about config.
_SECRET_MATERIAL_PATTERNS: Final[tuple[str, ...]] = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "service-account*.json",
)

_LINT_CONFIGS: Final[tuple[str, ...]] = (
    ".pre-commit-config.yaml",
    ".golangci.yml",
    ".golangci.yaml",
    "eslint.config.mjs",
    "eslint.config.js",
    ".eslintrc.json",
    ".eslintrc.js",
    "ruff.toml",
    ".ruff.toml",
    ".flake8",
    "setup.cfg",
)

_CI_PATTERNS: Final[tuple[str, ...]] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    ".circleci/config.yml",
    "jenkinsfile",
    ".drone.yml",
    "bitbucket-pipelines.yml",
    ".woodpecker.yml",
)

_COMPOSE_PATTERNS: Final[tuple[str, ...]] = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose.yml",
    "compose.yaml",
)

_IAC_PATTERNS: Final[tuple[str, ...]] = ("*.tf", "*.tofu", "*.tfvars", "*.bicep")

_ENV_EXAMPLE_PATTERNS: Final[tuple[str, ...]] = (
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
    "*.env.example",
)

#: `FROM` at the start of a line. A `FROM` inside a RUN heredoc is not a build stage, and
#: counting one would report a single-stage Dockerfile as multi-stage.
_FROM_LINE = re.compile(r"(?im)^\s*FROM\s+\S+")
#: `USER` with anything other than root/0. A `USER root` is not a non-root container, and
#: the check exists precisely to catch the image that sets USER and sets it to root.
_NON_ROOT_USER = re.compile(r"(?im)^\s*USER\s+(?!root\b|0\b)\S+")
_K8S_KIND = re.compile(r"(?im)^\s*kind:\s*(Deployment|StatefulSet|DaemonSet|CronJob|Service)\b")
_TF_BACKEND = re.compile(r"(?im)^\s*backend\s+\"")


class ReadinessCheck(BaseModel):
    """One checklist item (phases.md §1.4 "Checklist checks", "why it matters").

    `evidence` names the indexed path that satisfied the check, or is empty when nothing
    did. That is what makes a score auditable: a category at 40 without evidence is
    indistinguishable from a bug.
    """

    id: str
    category: str
    passed: bool
    points: int
    max_points: int
    evidence: str = ""
    why_it_matters: str


class ReadinessBreakdown(BaseModel):
    """The §1.4 categories, each 0-100.

    Percentages rather than the previous point contributions, because the readiness screen
    renders each value as a bar width in percent — a category worth 20 points rendered as
    "20%" was reporting its weight as its score.
    """

    containerization_score: int
    ci_config_score: int
    orchestration_score: int
    env_config_score: int
    security_policy_score: int
    iac_score: int


class ReadinessResult(BaseModel):
    overall_score: int
    level: str  # "production_ready", "needs_improvement", "blocked"
    breakdown: ReadinessBreakdown
    recommendations: list[str]
    checks: list[ReadinessCheck] = Field(default_factory=list)
    #: False when the project has no indexed files at all. A caller must be able to tell
    #: "scored zero" from "never scanned", and a score alone cannot.
    indexed: bool = True
    #: How many indexed paths the score was computed from. Determinism evidence for the
    #: person reading the number, not a metric.
    evaluated_paths: int = 0


# `IndexEvidence` lives in `core.index_evidence` and is re-exported here for the callers that
# read a score and its input together. It is defined there rather than here because `analysis`
# produces it and this module consumes it, and §2.2.1 bans those two domains from importing each
# other — the shared floor is `core`, and a type both sides name has to sit on it.


def _normalise(paths: Iterable[str]) -> tuple[str, ...]:
    """Lower-cased, slash-separated, de-duplicated, sorted.

    Sorted because the first matching path becomes the `evidence` string, and a score
    whose evidence changes between identical evaluations is not deterministic even when
    the number is.
    """
    seen = {p.replace("\\", "/").strip().removeprefix("./").lower() for p in paths if p and p.strip()}
    return tuple(sorted(seen - {""}))


def apply_ignore_globs(paths: Iterable[str], ignore_globs: Iterable[str] | None) -> tuple[str, ...]:
    """Drop paths the project's `ignore_globs` setting excludes.

    This is the ONLY way `projects.settings` may influence the score, and it is a
    refinement of the index rather than a substitute for it: the operator says which parts
    of the repository are out of scope, and the remaining evidence is still the
    repository's. A glob that matches everything yields an unindexed project, not a
    perfect one.
    """
    normalised = _normalise(paths)
    patterns = [g.replace("\\", "/").lower() for g in (ignore_globs or []) if isinstance(g, str) and g.strip()]
    if not patterns:
        return normalised
    kept = []
    for path in normalised:
        if any(fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*/") + "/") for pattern in patterns):
            continue
        kept.append(path)
    return tuple(kept)


def _match(paths: tuple[str, ...], patterns: Iterable[str]) -> str:
    """First path matching any pattern, by basename or by full path."""
    lowered = [p.lower() for p in patterns]
    for path in paths:
        base = posixpath.basename(path)
        for pattern in lowered:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern):
                return path
    return ""


def _match_exact(paths: tuple[str, ...], names: Iterable[str]) -> str:
    wanted = {n.lower() for n in names}
    for path in paths:
        if path in wanted or posixpath.basename(path) in wanted:
            return path
    return ""


def _dockerfile(paths: tuple[str, ...]) -> str:
    return _match(paths, ("dockerfile", "dockerfile.*", "*.dockerfile", "containerfile"))


def _content_of(evidence: IndexEvidence, paths: tuple[str, ...], patterns: Iterable[str]) -> str:
    """Concatenated bodies of every indexed path matching `patterns`.

    Concatenated rather than "the first one": a repository with a backend and a frontend
    Dockerfile is multi-stage if either is, and picking one arbitrarily would make the
    answer depend on sort order.
    """
    lowered = [p.lower() for p in patterns]
    bodies = []
    for path in paths:
        base = posixpath.basename(path)
        if any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(base, p) for p in lowered):
            body = evidence.contents.get(path)
            if body:
                bodies.append(body)
    return "\n".join(bodies)


def _has_test_evidence(paths: tuple[str, ...]) -> str:
    found = _match(paths, _TEST_PATTERNS)
    if found:
        return found
    for path in paths:
        segments = path.split("/")[:-1]
        if any(segment in _TEST_DIRECTORIES for segment in segments):
            return path
    return ""


class ReadinessEngine:
    """Deterministic readiness scoring engine.

    Deterministic in the strict sense: the score is a pure function of the evidence, with
    no clock, no randomness and no iteration over an unordered set. `analysis_reports`
    stores the score alongside an `inventory_hash` so two reports can be compared, and
    that comparison is only meaningful if identical evidence scores identically.
    """

    def evaluate(self, evidence: IndexEvidence) -> ReadinessResult:
        """Score indexed repository evidence."""
        paths = _normalise(evidence.paths)
        checks: list[ReadinessCheck] = []

        # ─── Containerization (§1.4) ─────────────────────────────────────────
        dockerfile = _dockerfile(paths)
        docker_body = _content_of(evidence, paths, ("dockerfile", "dockerfile.*", "*.dockerfile", "containerfile"))
        checks.append(
            self._check(
                "dockerfile_present",
                "containerization",
                bool(dockerfile),
                40,
                dockerfile,
                "Without a Dockerfile the deployment target is whatever the last person's machine had installed.",
            )
        )
        multi_stage = len(_FROM_LINE.findall(docker_body)) >= 2
        checks.append(
            self._check(
                "dockerfile_multi_stage",
                "containerization",
                multi_stage,
                25,
                dockerfile if multi_stage else "",
                "A single-stage image ships the compiler and the source alongside the binary, "
                "which is both a larger image and a larger attack surface.",
            )
        )
        non_root = bool(_NON_ROOT_USER.search(docker_body))
        checks.append(
            self._check(
                "dockerfile_non_root",
                "containerization",
                non_root,
                20,
                dockerfile if non_root else "",
                "A container with no USER runs as root, so a process escape starts with root in the namespace.",
            )
        )
        # The two remaining checks the design's Phase 1 list names — "pins a base image digest" and
        # "has a `HEALTHCHECK`" — neither of which existed. Both read INSTRUCTIONS rather than search the
        # text, so a commented-out `HEALTHCHECK` and a `FROM` in a comment do not count.
        base_pinned = dockerfile_base_pinned(docker_body)
        checks.append(
            self._check(
                "dockerfile_base_pinned",
                "containerization",
                base_pinned,
                20,
                dockerfile if base_pinned else "",
                "An unpinned base means two builds of one Dockerfile can produce different images.",
            )
        )
        healthcheck = dockerfile_healthcheck(docker_body)
        checks.append(
            self._check(
                "dockerfile_healthcheck_present",
                "containerization",
                healthcheck,
                15,
                dockerfile if healthcheck else "",
                "Without a HEALTHCHECK the runtime cannot tell a wedged container from a busy one.",
            )
        )
        dockerignore = _match_exact(paths, (".dockerignore",))
        checks.append(
            self._check(
                "dockerignore_present",
                "containerization",
                bool(dockerignore),
                15,
                dockerignore,
                "Without .dockerignore the build context carries .git and local secrets into the image layer cache.",
            )
        )

        # ─── CI/CD (§1.4) ────────────────────────────────────────────────────
        ci = _match(paths, _CI_PATTERNS)
        checks.append(
            self._check(
                "ci_pipeline_present",
                "ci_config",
                bool(ci),
                45,
                ci,
                "Without a pipeline definition nothing is verified before a change reaches a branch.",
            )
        )
        test_path = _has_test_evidence(paths)
        has_tests = evidence.has_tests if evidence.has_tests is not None else bool(test_path)
        checks.append(
            self._check(
                "automated_tests_present",
                "ci_config",
                has_tests,
                35,
                test_path,
                "A pipeline with no tests to run reports green for every change, which is worse than no pipeline.",
            )
        )
        lint = _match_exact(paths, _LINT_CONFIGS)
        checks.append(
            self._check(
                "lint_configuration_present",
                "ci_config",
                bool(lint),
                20,
                lint,
                "A committed linter configuration is what makes style and a class of bugs a machine's problem.",
            )
        )

        # ─── FR-20's "pipeline stages" ────────────────────────────────────────
        #
        # `ci_pipeline_present` matched a PATH, so a workflow file with a `jobs:` key and no steps
        # satisfied it while running nothing. These three read the parsed workflow instead.
        stages_ok, stages_evidence = pipeline_stages_declared(paths, evidence.contents)
        checks.append(
            self._check(
                "pipeline_stages_declared",
                "ci_config",
                stages_ok,
                25,
                stages_evidence,
                "A workflow with no runnable step and no trigger is a file, not a pipeline.",
            )
        )
        ci_tests_ok, ci_tests_evidence = pipeline_runs_tests(paths, evidence.contents)
        checks.append(
            self._check(
                "pipeline_runs_tests",
                "ci_config",
                ci_tests_ok,
                30,
                ci_tests_evidence,
                "A pipeline that does not run the tests reports green for every change.",
            )
        )
        # Scored only when a workflow exists, for the reason the orchestration block gives: a repository
        # with no CI already fails `ci_pipeline_present`, and a second failure would misdescribe why.
        pinned_ok, pinned_evidence = pipeline_actions_pinned(paths, evidence.contents)
        checks.append(
            self._check(
                "pipeline_actions_pinned",
                "ci_config",
                pinned_ok if ci else False,
                15,
                # `or ci` is the evidence fallback for the case where the workflow PATH is indexed but its
                # body is not available to this evaluation: the check then has nothing to examine and no
                # finding to cite, and `test_every_check_that_passes_names_its_evidence` requires a
                # passing check to name something. The workflow is the honest citation, since it is the
                # file the answer is about.
                pinned_evidence or ci,
                "A tag is mutable, so an unpinned action is code this repository does not control.",
            )
        )

        # ─── Orchestration (§1.4) ────────────────────────────────────────────
        k8s_body = _content_of(evidence, paths, ("*.yaml", "*.yml"))
        k8s_path = _match(paths, ("k8s/*", "kubernetes/*", "deploy/*.yaml", "manifests/*.yaml"))
        has_k8s = bool(_K8S_KIND.search(k8s_body)) or bool(k8s_path)
        checks.append(
            self._check(
                "kubernetes_manifests_present",
                "orchestration",
                has_k8s,
                45,
                k8s_path,
                "Without a manifest the runtime shape - replicas, probes, limits - lives only in a shell history.",
            )
        )
        helm = _match_exact(paths, ("chart.yaml",))
        checks.append(
            self._check(
                "helm_chart_present",
                "orchestration",
                bool(helm),
                30,
                helm,
                "A chart is what makes the same manifests deployable to another environment unedited.",
            )
        )
        compose = _match(paths, _COMPOSE_PATTERNS)
        checks.append(
            self._check(
                "compose_file_present",
                "orchestration",
                bool(compose),
                25,
                compose,
                "A compose file is the reproducible local topology; without one, 'works here' is unfalsifiable.",
            )
        )

        # ─── FR-20's missing orchestration checks ─────────────────────────────
        #
        # These three had no implementation. A manifest with no `resources` block, no probes and
        # `image: app:latest` scored full marks for orchestration, because the only question asked was
        # whether a manifest existed. FR-20 names "K8s resource limits" explicitly.
        #
        # Each is scored ONLY when a manifest exists. Failing a repository with no Kubernetes for having
        # no resource limits would double-count the absence: `kubernetes_manifests_present` already
        # reports it, and the second failure would say the manifests are wrong rather than absent.
        limits_ok, limits_evidence = kubernetes_resource_limits(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_resource_limits_declared",
                "orchestration",
                limits_ok if has_k8s else False,
                35,
                limits_evidence,
                "An unbounded container evicts its neighbours; one missing limit is enough to take a node.",
            )
        )
        probes_ok, probes_evidence = kubernetes_probes(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_probes_declared",
                "orchestration",
                probes_ok if has_k8s else False,
                25,
                probes_evidence,
                "Without probes a wedged container keeps serving traffic, because nothing is asking it.",
            )
        )
        tags_ok, tags_evidence = kubernetes_image_tags_pinned(paths, evidence.contents)
        checks.append(
            self._check(
                "kubernetes_image_tags_pinned",
                "orchestration",
                tags_ok if has_k8s else False,
                20,
                tags_evidence,
                "A `latest` tag means two deployments of one manifest can run different code.",
            )
        )

        # ─── Env Config (§1.4) ───────────────────────────────────────────────
        env_example = _match(paths, _ENV_EXAMPLE_PATTERNS)
        checks.append(
            self._check(
                "env_example_present",
                "env_config",
                bool(env_example),
                40,
                env_example,
                "Without a checked-in example, the variables the service needs are found only by crashing it.",
            )
        )
        committed_env = _match_exact(paths, (".env",))
        checks.append(
            self._check(
                "no_committed_env_file",
                "env_config",
                # `and bool(paths)`: this and `no_committed_key_material` are the two checks
                # whose passing condition is an ABSENCE, and an empty index satisfies every
                # absence trivially. Without this an unscanned project scored 8/100 for the
                # bad files it did not have, which is a conclusion drawn from no evidence.
                not committed_env and bool(paths),
                35,
                committed_env,
                "A committed .env puts live credentials in every clone and in the image build context.",
            )
        )
        config_dir = _match(paths, ("config/*", "configs/*", "*/settings.py", "*/config.py", "*.config.ts"))
        checks.append(
            self._check(
                "centralised_configuration",
                "env_config",
                bool(config_dir),
                25,
                config_dir,
                "Configuration read in one validated place fails at boot; read ad hoc it fails in production.",
            )
        )

        # ─── Security (§1.4) ─────────────────────────────────────────────────
        policy = _match(paths, ("security.md", "policies/*", "*.rego", "policies/*.yaml"))
        checks.append(
            self._check(
                "security_policy_present",
                "security_policy",
                bool(policy),
                30,
                policy,
                "A written policy is what turns a security decision into something reviewable rather than remembered.",
            )
        )
        secret_scanning = _match_exact(paths, (".gitleaks.toml", ".trufflehog.yaml", ".secrets.baseline"))
        checks.append(
            self._check(
                "secret_scanning_configured",
                "security_policy",
                bool(secret_scanning),
                25,
                secret_scanning,
                "Secret scanning in the repository is the only control that catches a credential before it is pushed.",
            )
        )
        lockfile = _match_exact(paths, _LOCKFILES)
        checks.append(
            self._check(
                "dependency_lockfile_present",
                "security_policy",
                bool(lockfile),
                25,
                lockfile,
                "Without a lockfile the dependency tree differs per build, so a fix cannot be proven applied.",
            )
        )
        key_material = _match(paths, _SECRET_MATERIAL_PATTERNS)
        checks.append(
            self._check(
                "no_committed_key_material",
                "security_policy",
                # See `no_committed_env_file`: an absence is not evidence when nothing was read.
                not key_material and bool(paths),
                20,
                key_material,
                "A private key in the tree is compromised the moment the repository is cloned, and history keeps it.",
            )
        )

        # ─── IaC (§1.4) ──────────────────────────────────────────────────────
        iac = _match(paths, _IAC_PATTERNS)
        checks.append(
            self._check(
                "iac_sources_present",
                "iac",
                bool(iac),
                50,
                iac,
                "Infrastructure defined in code is reviewable and revertible; a console click is neither.",
            )
        )
        iac_lock = _match_exact(paths, (".terraform.lock.hcl",))
        checks.append(
            self._check(
                "iac_provider_lock_present",
                "iac",
                bool(iac_lock),
                25,
                iac_lock,
                "Without a provider lock, the same plan can produce different infrastructure on different days.",
            )
        )
        iac_body = _content_of(evidence, paths, _IAC_PATTERNS)
        remote_state = bool(_TF_BACKEND.search(iac_body))
        checks.append(
            self._check(
                "iac_remote_state_configured",
                "iac",
                remote_state,
                25,
                iac if remote_state else "",
                "Local state cannot be shared or locked, so two applies can silently overwrite each other.",
            )
        )

        category_scores = self._category_scores(checks)
        overall = round(sum(CATEGORY_WEIGHTS[c] * category_scores[c] for c in CATEGORY_WEIGHTS) / 100)
        overall = min(max(overall, 0), 100)

        if overall >= 80:
            level = "production_ready"
        elif overall >= 50:
            level = "needs_improvement"
        else:
            level = "blocked"

        return ReadinessResult(
            overall_score=overall,
            level=level,
            breakdown=ReadinessBreakdown(
                **{CATEGORY_FIELDS[name]: score for name, score in category_scores.items()},
            ),
            recommendations=self._recommendations(checks, indexed=bool(paths)),
            checks=checks,
            indexed=bool(paths),
            evaluated_paths=len(paths),
        )

    def evaluate_project(self, project_data: dict[str, Any]) -> ReadinessResult:
        """Score a project from a mapping of evidence.

        Retained as the engine's dict-shaped entry point, but its meaning has changed: the
        keys are now READ AS PATHS from the index, not as configuration. `paths` is the
        preferred key; `manifests` and `config_files` are accepted because they are what
        the agent inventory calls the same thing, and `has_tests` is honoured when stated
        and DERIVED when absent — it no longer defaults to true.
        """
        paths: list[str] = []
        for key in ("paths", "manifests", "config_files"):
            value = project_data.get(key)
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, Iterable):
                paths.extend(str(v) for v in value)

        stated = project_data.get("has_tests")
        contents = project_data.get("contents")
        return self.evaluate(
            IndexEvidence(
                paths=tuple(paths),
                contents=dict(contents) if isinstance(contents, Mapping) else {},
                has_tests=stated if isinstance(stated, bool) else None,
            )
        )

    @staticmethod
    def _check(
        check_id: str,
        category: str,
        passed: bool,
        max_points: int,
        evidence: str,
        why_it_matters: str,
    ) -> ReadinessCheck:
        return ReadinessCheck(
            id=check_id,
            category=category,
            passed=passed,
            points=max_points if passed else 0,
            max_points=max_points,
            # The path that DECIDED the check, whichever way it went. For a positive check
            # that is the file that satisfied it, and is empty when nothing did; for an
            # absence check (`no_committed_env_file`) it is the offending file, which is the
            # single most useful thing a failed check can report.
            evidence=evidence,
            why_it_matters=why_it_matters,
        )

    @staticmethod
    def _category_scores(checks: list[ReadinessCheck]) -> dict[str, int]:
        scores: dict[str, int] = {}
        for category in CATEGORY_WEIGHTS:
            earned = sum(c.points for c in checks if c.category == category)
            possible = sum(c.max_points for c in checks if c.category == category)
            # `possible` is a constant of the check table, never zero; guarded anyway
            # because a category whose checks were all removed should score 0 rather
            # than raise.
            scores[category] = round(100 * earned / possible) if possible else 0
        return scores

    @staticmethod
    def _recommendations(checks: list[ReadinessCheck], *, indexed: bool) -> list[str]:
        """Failed checks as plain-language actions, heaviest category first.

        The unindexed case is called out explicitly rather than producing eighteen
        recommendations: telling an operator to add a Dockerfile when the repository has
        never been scanned is advice about a repository nobody has read.
        """
        if not indexed:
            return [
                "No indexed files for this project: run an agent scan so readiness is measured "
                "from the repository rather than assumed."
            ]
        failed = [c for c in checks if not c.passed]
        failed.sort(key=lambda c: (-CATEGORY_WEIGHTS.get(c.category, 0), -c.max_points, c.id))
        return [f"{c.why_it_matters} (check: {c.id})" for c in failed]
