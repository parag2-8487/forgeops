# SPDX-License-Identifier: FSL-1.1-ALv2
"""What each readiness check looked for, where, and the concrete change that satisfies it.

WHY THIS EXISTS

A failing check used to report this:

    Fail iac_remote_state_configured
    0 of 25 points
    Evidence:
    Why it matters: Local state cannot be shared or locked, so two applies can silently overwrite
    each other.

The empty `Evidence:` was not an oversight in one check. Twenty-nine call sites passed
`evidence = <path> if passed else ""`, so evidence was deliberately blanked on failure — the one
outcome where a reader needs it most. "Why it matters" explains the principle and says nothing about
this repository, so a user who did not already know the answer was told they had a problem and given
no way to find or fix it.

Every entry here answers five questions for one check:

  * `looked_for` — what would satisfy it, in this repository's terms
  * `looked_in`  — where it searched, so "not found" is falsifiable
  * `remedy`     — the change to make, as text that can be pasted
  * `remedy_path`— the file to put it in, or the convention when the path depends on the project
  * `fixable`    — whether generation can produce it, and if not, precisely why

WHY THE TABLE IS DATA AND NOT PROSE IN THE ENGINE

Two consumers need the same answer and must not disagree. The readiness screen renders it to explain a
failure, and the generation surface reads `fixable` to decide what it can offer — which is what
replaces the blanket "Generation cannot raise this score" with a per-check statement. A second copy of
this reasoning would drift, and the two surfaces would then contradict each other in front of a user.

`found` is NOT here, deliberately. It is the one part that depends on what is actually in the tree, so
it comes from the call site that looked. A table entry that claimed to know what was found would be
inventing evidence.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

#: The artifact kind that satisfies a check, or `""` when no single artifact does.
#:
#: WHY THIS EXISTS RATHER THAN A HAND-MAINTAINED BOOLEAN. `Fixability.generatable` says whether a
#: check is generatable IN PRINCIPLE — whether the fix is a file that can be written at all. Whether
#: generation can produce it TODAY is a different question, answered by which artifact kinds the
#: generator actually emits. Recording only the principle would let the readiness screen promise a CI
#: workflow the generator cannot write, which is the same class of untruth as the blanket disclaimer
#: it replaces, pointing the other way.
#:
#: The two facts are combined by `is_generatable_today`, so the screen's claim moves automatically as
#: the generator learns a kind, and cannot be left behind or run ahead of it.
ArtifactKind = str

#: Every kind a check can name. Checked against `GENERATED_ARTIFACT_KINDS` by a test, so a typo
#: becomes a failure rather than a check that silently claims nothing can fix it.
KNOWN_ARTIFACT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "dockerfile",
        "dockerignore",
        "k8s",
        "helm",
        "compose",
        "github_workflow",
        "env_example",
        "opentofu",
        "lint_config",
        "security_policy",
        "secret_scanner_config",
    }
)

#: The kinds the generator emits today, mirroring `generation/schemas.py`.
#:
#: `test_the_emitted_kinds_match_the_generation_schemas` keeps this honest: it reads the `Literal`
#: values off the artifact models, so adding a schema without adding it here — or the reverse — fails.
GENERATED_ARTIFACT_KINDS: Final[frozenset[str]] = frozenset({"dockerfile", "k8s"})


class Fixability(BaseModel):
    """Whether generation can satisfy a check, and the honest reason when it cannot."""

    #: True when a change set can fully satisfy the check.
    generatable: bool
    #: Present only when `generatable` is False. States what a generator cannot know or cannot do —
    #: never "not supported yet", which is a schedule rather than a reason.
    blocked_because: str = ""
    #: The nearest safe thing generation CAN offer when it cannot do the whole job. Empty when the
    #: check is fully generatable or when nothing partial would help.
    partial_offer: str = ""

    model_config = {"frozen": True}


class CheckExplanation(BaseModel):
    """The fixed, repository-independent half of a check's explanation."""

    looked_for: str
    looked_in: str
    remedy: str
    remedy_path: str
    fixability: Fixability
    #: The artifact whose generation would satisfy this check, or "" when no single artifact does.
    artifact: str = ""

    model_config = {"frozen": True}

    @property
    def generatable_today(self) -> bool:
        """Whether the generator can satisfy this check NOW, not merely in principle.

        Both halves have to hold: the fix must be a file that can be written at all, and the generator
        must actually emit that kind of file. Reporting the first alone would have the readiness screen
        offer a CI workflow the generator cannot produce — the blanket disclaimer's mistake inverted.
        """
        return self.fixability.generatable and self.artifact in GENERATED_ARTIFACT_KINDS


def _gen() -> Fixability:
    return Fixability(generatable=True)


#: Keyed by check id. `test_every_check_id_has_an_explanation` fails the build if a check is added to
#: the engine without an entry, which is what stops the empty-evidence class of defect coming back.
CHECK_EXPLANATIONS: Final[dict[str, CheckExplanation]] = {
    # ─── Containerization ────────────────────────────────────────────────────
    "dockerfile_present": CheckExplanation(
        looked_for="a Dockerfile anywhere in the repository",
        looked_in="Dockerfile, Dockerfile.*, *.Dockerfile, Containerfile",
        remedy=(
            "Add a Dockerfile that builds this project. It must name a base image matching the "
            "language the scan detected, copy the dependency manifest before the source so the "
            "layer cache survives a code change, and end with the command that starts the service."
        ),
        remedy_path="Dockerfile",
        fixability=_gen(),
        artifact="dockerfile",
    ),
    "dockerfile_multi_stage": CheckExplanation(
        looked_for="two or more FROM instructions, so build tooling does not ship in the final image",
        looked_in="the FROM instructions of the Dockerfile that was found",
        remedy=(
            "Split the build in two. Give the first stage a name and let it compile or install:\n\n"
            "    FROM <base> AS build\n"
            "    WORKDIR /src\n"
            "    COPY <manifest> .\n"
            "    RUN <install or build command>\n"
            "    COPY . .\n"
            "    RUN <build command>\n\n"
            "    FROM <slim runtime base>\n"
            "    WORKDIR /app\n"
            "    COPY --from=build /src/<build output> ./\n"
            '    CMD ["<start command>"]\n\n'
            "Only the second stage becomes the image, so the compiler, the package cache and the "
            "sources stay behind."
        ),
        remedy_path="Dockerfile",
        fixability=_gen(),
        artifact="dockerfile",
    ),
    "dockerfile_non_root": CheckExplanation(
        looked_for="a USER instruction naming a non-root account",
        looked_in="the USER instructions of the Dockerfile that was found",
        remedy=(
            "Create an unprivileged account and switch to it before the entrypoint, after everything "
            "that needs to write as root has run:\n\n"
            "    RUN useradd --system --uid 10001 --no-create-home app\n"
            "    USER 10001\n\n"
            "A numeric id is used rather than a name because Kubernetes' runAsNonRoot check reads the "
            "numeric uid and cannot resolve a name from the image."
        ),
        remedy_path="Dockerfile",
        fixability=_gen(),
        artifact="dockerfile",
    ),
    "dockerfile_base_pinned": CheckExplanation(
        looked_for="every FROM pinned to a digest or an exact version, never a floating tag",
        looked_in="the FROM instructions of the Dockerfile that was found",
        remedy=(
            "Replace the floating tag with a digest, which is the only immutable reference:\n\n"
            "    FROM <image>@sha256:<64 hex characters>\n\n"
            "Obtain it with `docker buildx imagetools inspect <image>:<tag>`. An exact version tag "
            "such as `3.12.4-slim` is an acceptable second best; `latest`, `3` and `3.12` are all "
            "republished under the same name and are therefore not references at all."
        ),
        remedy_path="Dockerfile",
        fixability=_gen(),
        artifact="dockerfile",
    ),
    "dockerfile_healthcheck_present": CheckExplanation(
        looked_for="a HEALTHCHECK instruction",
        looked_in="the HEALTHCHECK instructions of the Dockerfile that was found",
        remedy=(
            "Add a HEALTHCHECK that exercises the path the service actually serves:\n\n"
            "    HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \\\n"
            "      CMD <command that exits non-zero when the service is not answering>\n\n"
            "Probe the application, not the process: a wedged process is still running."
        ),
        remedy_path="Dockerfile",
        fixability=_gen(),
        artifact="dockerfile",
    ),
    "dockerignore_present": CheckExplanation(
        looked_for="a .dockerignore file",
        looked_in=".dockerignore at the repository root",
        remedy=(
            "Add a .dockerignore excluding at least the version-control directory, local "
            "environment files and installed dependencies:\n\n"
            "    .git\n"
            "    .env\n"
            "    node_modules\n"
            "    __pycache__\n"
            "    *.pem\n"
            "    *.key\n\n"
            "Without it the whole working tree is sent to the daemon and can be committed into a "
            "layer, including files the repository itself ignores."
        ),
        remedy_path=".dockerignore",
        fixability=_gen(),
        artifact="dockerignore",
    ),
    # ─── CI/CD ───────────────────────────────────────────────────────────────
    "ci_pipeline_present": CheckExplanation(
        looked_for="a pipeline definition for any supported provider",
        looked_in=".github/workflows/*.yml, .gitlab-ci.yml, Jenkinsfile, azure-pipelines.yml",
        remedy=(
            "Add a workflow that runs on push and on pull request, installs dependencies, and runs "
            "the build and the tests. It must fail the job when a step fails — a workflow that "
            "cannot go red verifies nothing."
        ),
        remedy_path=".github/workflows/ci.yml",
        fixability=_gen(),
        artifact="github_workflow",
    ),
    "automated_tests_present": CheckExplanation(
        looked_for="test files or a test directory",
        looked_in="tests/, test/, spec/, __tests__/, *_test.*, *.test.*, *.spec.*",
        remedy=(
            "Add tests. This is the one item on this report that cannot be satisfied by "
            "configuration: a test asserts something about behaviour that only somebody who knows "
            "the intended behaviour can write."
        ),
        remedy_path="tests/",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "a test encodes what the code is SUPPOSED to do, and nothing in the repository states "
                "that. A generated test would assert current behaviour, so it would pass on a bug and "
                "lock it in — the check would go green while the repository got worse."
            ),
            partial_offer=(
                "a test harness and one honest smoke test that starts the application and asserts it "
                "answers, which is checkable without knowing the domain"
            ),
        ),
        artifact="",
    ),
    "lint_configuration_present": CheckExplanation(
        looked_for="a committed linter or formatter configuration",
        looked_in=".eslintrc*, eslint.config.*, ruff.toml, .flake8, .golangci.yml, pyproject.toml",
        remedy=(
            "Add a linter configuration for the language the scan detected and commit it, so every "
            "contributor and the pipeline apply the same rules rather than each machine's defaults."
        ),
        remedy_path="(depends on language: ruff.toml, eslint.config.mjs, .golangci.yml)",
        fixability=_gen(),
        artifact="lint_config",
    ),
    "pipeline_stages_declared": CheckExplanation(
        looked_for="at least one runnable step and at least one trigger in the workflow",
        looked_in="the parsed body of every workflow file that was found",
        remedy=(
            "Give the workflow a trigger and real steps:\n\n"
            "    on:\n"
            "      push:\n"
            "        branches: [main]\n"
            "      pull_request:\n"
            "    jobs:\n"
            "      build:\n"
            "        runs-on: ubuntu-latest\n"
            "        steps:\n"
            "          - uses: actions/checkout@<sha>\n"
            "          - run: <install>\n"
            "          - run: <build>\n\n"
            "A `jobs:` key whose jobs have no steps parses as valid YAML and runs nothing."
        ),
        remedy_path=".github/workflows/ci.yml",
        fixability=_gen(),
        artifact="github_workflow",
    ),
    "pipeline_runs_tests": CheckExplanation(
        looked_for="a step that invokes a test runner",
        looked_in="the run: and uses: values of every step in every workflow that was found",
        remedy=(
            "Add a step that runs the test suite and let it fail the job:\n\n"
            "          - run: <the project's test command>\n\n"
            "Do not append `|| true` or `continue-on-error: true`; both turn the job green when the "
            "tests fail, which is worse than not running them because it looks like assurance."
        ),
        remedy_path=".github/workflows/ci.yml",
        fixability=_gen(),
        artifact="github_workflow",
    ),
    "pipeline_actions_pinned": CheckExplanation(
        looked_for="every `uses:` pinned to a full 40-character commit SHA",
        looked_in="the uses: values of every step in every workflow that was found",
        remedy=(
            "Replace each floating reference with the commit it currently resolves to, keeping the "
            "human-readable version in a trailing comment:\n\n"
            "    - uses: actions/checkout@<40-hex-sha>  # v4.2.2\n\n"
            "A tag is a moving pointer the action's owner can repoint at any time, so an unpinned "
            "action is third-party code that can change under a repository that never changed."
        ),
        remedy_path=".github/workflows/",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the substitution can be written for every unpinned action, but the SHA each tag "
                "currently points at has to be resolved against the action's repository"
            ),
        ),
        artifact="github_workflow",
    ),
    # ─── Orchestration ───────────────────────────────────────────────────────
    "kubernetes_manifests_present": CheckExplanation(
        looked_for="at least one manifest declaring a Kubernetes kind",
        looked_in="k8s/*, kubernetes/*, manifests/*, deploy/*.yaml, deployment/*.yaml",
        remedy=(
            "Add a Deployment and a Service for the container this repository builds. The Deployment "
            "must name the same image the pipeline pushes, or the two describe different software."
        ),
        remedy_path="k8s/deployment.yaml, k8s/service.yaml",
        fixability=_gen(),
        artifact="k8s",
    ),
    "helm_chart_present": CheckExplanation(
        looked_for="a Chart.yaml",
        looked_in="Chart.yaml anywhere in the repository",
        remedy=(
            "Add a chart so the same manifests can be deployed to another environment without being "
            "edited:\n\n"
            "    charts/<name>/Chart.yaml\n"
            "    charts/<name>/values.yaml\n"
            "    charts/<name>/templates/\n\n"
            "Values that differ per environment — image tag, replica count, host — belong in "
            "values.yaml rather than in the template."
        ),
        remedy_path="charts/<name>/Chart.yaml",
        fixability=_gen(),
        artifact="helm",
    ),
    "compose_file_present": CheckExplanation(
        looked_for="a Docker Compose file",
        looked_in="docker-compose.yml, docker-compose.yaml, compose.yml, compose.yaml",
        remedy=(
            "Add a compose file describing the service and every backing service it needs, so a "
            "contributor can start the whole topology with one command and 'works on my machine' "
            "becomes a falsifiable claim."
        ),
        remedy_path="docker-compose.yml",
        fixability=_gen(),
        artifact="compose",
    ),
    "kubernetes_resource_limits_declared": CheckExplanation(
        looked_for="requests and limits for cpu and memory on every container",
        looked_in="every container spec in every manifest that was found",
        remedy=(
            "Declare both on each container:\n\n"
            "        resources:\n"
            "          requests:\n"
            "            cpu: 100m\n"
            "            memory: 128Mi\n"
            "          limits:\n"
            "            cpu: 500m\n"
            "            memory: 512Mi\n\n"
            "Requests are what the scheduler reserves; limits are what stops one container taking a "
            "node from its neighbours. One container without them is enough to lose the node."
        ),
        remedy_path="k8s/deployment.yaml",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the block can be added to every container that lacks it, but the numbers are a "
                "starting point rather than a measurement — right-sizing needs observed usage"
            ),
        ),
        artifact="k8s",
    ),
    "kubernetes_probes_declared": CheckExplanation(
        looked_for="a livenessProbe and a readinessProbe on every container",
        looked_in="every container spec in every manifest that was found",
        remedy=(
            "Add both, pointing at a path the service really serves:\n\n"
            "        livenessProbe:\n"
            "          httpGet: { path: /health, port: <port> }\n"
            "          initialDelaySeconds: 10\n"
            "        readinessProbe:\n"
            "          httpGet: { path: /health/ready, port: <port> }\n\n"
            "They answer different questions: readiness gates traffic, liveness restarts. Without "
            "them a wedged container keeps receiving requests because nothing is asking it anything."
        ),
        remedy_path="k8s/deployment.yaml",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the probe blocks can be written, but the path and port have to match a health "
                "endpoint the application actually exposes — if it has none, one has to be added to "
                "the source first"
            ),
        ),
        artifact="k8s",
    ),
    "kubernetes_image_tags_pinned": CheckExplanation(
        looked_for="no container image referencing `latest` or carrying no tag at all",
        looked_in="the image: values of every container in every manifest that was found",
        remedy=(
            "Replace the floating reference with an immutable one:\n\n"
            "        image: <registry>/<name>@sha256:<64 hex characters>\n\n"
            "or, at least, the exact version the pipeline pushed. With `latest`, two applies of one "
            "unchanged manifest can run different code, which makes a rollback meaningless."
        ),
        remedy_path="k8s/deployment.yaml",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the manifest can be rewritten to read the tag from a single value, but the digest to "
                "pin to is produced by the build and is not knowable from the repository alone"
            ),
        ),
        artifact="k8s",
    ),
    # ─── Env Config ──────────────────────────────────────────────────────────
    "env_example_present": CheckExplanation(
        looked_for="a checked-in example environment file",
        looked_in=".env.example, .env.sample, .env.template, example.env",
        remedy=(
            "Add a .env.example naming every variable the service reads, with a safe placeholder or "
            "an empty value and a comment for each. Never a real credential — this file is committed.\n\n"
            "    # The Postgres connection string the service reads at boot.\n"
            "    DATABASE_URL=\n"
            "    # Log verbosity: debug, info, warn, error.\n"
            "    LOG_LEVEL=info\n\n"
            "Without it the required variables are discovered by starting the service and watching "
            "it crash."
        ),
        remedy_path=".env.example",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the variable names can be read out of the source, but whether each is required and "
                "what a safe default is are judgements the code does not state"
            ),
        ),
        artifact="env_example",
    ),
    "no_committed_env_file": CheckExplanation(
        looked_for="no .env committed to the repository",
        looked_in=".env at the repository root",
        remedy=(
            "Remove it from the index and from history, then ignore it:\n\n"
            "    git rm --cached .env\n"
            "    printf '.env\\n' >> .gitignore\n\n"
            "Then TREAT EVERY VALUE IT CONTAINED AS DISCLOSED and rotate them. Deleting the file "
            "does not remove it from clones already taken or from the commits that carried it."
        ),
        remedy_path=".gitignore",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "the fix is a deletion plus a credential rotation. A change set can add the ignore "
                "rule, but rewriting history and rotating live secrets are irreversible operations "
                "outside a working tree, and doing them automatically could lock you out of your own "
                "infrastructure."
            ),
            partial_offer="the .gitignore entry, and a list of the variable names that need rotating",
        ),
        artifact="",
    ),
    "centralised_configuration": CheckExplanation(
        looked_for="a single place where configuration is read and validated",
        looked_in="config/*, configs/*, */settings.py, */config.py, *.config.ts",
        remedy=(
            "Read every variable in one module, validate it there, and let the process fail at boot "
            "if something required is missing or malformed. Then have the rest of the code take "
            "values from that module rather than reading the environment directly.\n\n"
            "Configuration read in one validated place fails immediately and visibly; read ad hoc it "
            "fails later, in whichever request first needs the missing value."
        ),
        remedy_path="(depends on language: src/config.py, src/config.ts, internal/config/config.go)",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "this is a refactor of code that already exists. The module can be written, but every "
                "existing read has to be found and rerouted through it, and a mechanical rewrite of "
                "those call sites can change behaviour where a default differs — a judgement that "
                "belongs to whoever knows the service."
            ),
            partial_offer=(
                "the validated config module itself, listing every variable the scan found being "
                "read, so the remaining work is redirecting the call sites"
            ),
        ),
        artifact="",
    ),
    # ─── Security ────────────────────────────────────────────────────────────
    "security_policy_present": CheckExplanation(
        looked_for="a written security policy or a policy-as-code source",
        looked_in="SECURITY.md, policies/*, *.rego, policies/*.yaml",
        remedy=(
            "Add a SECURITY.md stating which versions are supported, how to report a vulnerability "
            "privately, and how quickly a reporter can expect an answer. Without it a finder's only "
            "options are a public issue or silence."
        ),
        remedy_path="SECURITY.md",
        fixability=_gen(),
        artifact="security_policy",
    ),
    "secret_scanning_configured": CheckExplanation(
        looked_for="a committed secret-scanner configuration",
        looked_in=".gitleaks.toml, .trufflehog.yaml, .secrets.baseline",
        remedy=(
            "Add a scanner configuration and run it in the pipeline and in a pre-commit hook. It is "
            "the only control that catches a credential BEFORE it is pushed; everything after that "
            "is rotation."
        ),
        remedy_path=".gitleaks.toml",
        fixability=_gen(),
        artifact="secret_scanner_config",
    ),
    "no_secrets_found_by_scan": CheckExplanation(
        looked_for="no file whose contents the scanner had to redact",
        looked_in="the redaction count recorded for every indexed file during the scan",
        remedy=(
            "Go to the reported file, remove the literal value, and read it from the environment "
            "instead. Then ROTATE IT: a credential that has been in a repository is disclosed to "
            "everyone who has ever cloned it, and removing the line does not recall those copies.\n\n"
            "The value itself is not shown here and was never stored — only the count and the path."
        ),
        remedy_path="(the reported file)",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "the secret was redacted at scan time and never stored, so nothing here knows what to "
                "replace or what the correct value is. Rotation happens at the system that issued the "
                "credential, which is outside this repository entirely."
            ),
            partial_offer=(
                "the path and the number of findings, so the line can be found, plus the .env.example "
                "entry the value should move to"
            ),
        ),
        artifact="",
    ),
    "dependency_lockfile_present": CheckExplanation(
        looked_for="a lockfile for the package manager the scan detected",
        looked_in=(
            "package-lock.json, pnpm-lock.yaml, yarn.lock, poetry.lock, uv.lock, Cargo.lock, "
            "go.sum, Gemfile.lock, composer.lock"
        ),
        remedy=(
            "Resolve the dependency tree once and commit the result. Without a lockfile each build "
            "resolves versions afresh, so a build that works and a build that fails can come from "
            "identical source, and a security fix cannot be proven to have been applied."
        ),
        remedy_path="(depends on package manager)",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "a lockfile is the OUTPUT of the package manager resolving against a live registry. "
                "Writing one by hand would state hashes nothing verified, which is worse than having "
                "none because it looks authoritative."
            ),
            partial_offer="the exact command to run for the detected package manager",
        ),
        artifact="",
    ),
    "no_committed_key_material": CheckExplanation(
        looked_for="no private key or certificate bundle committed to the repository",
        looked_in="*.pem, *.key, *_rsa, *.p12, *.pfx, id_rsa, id_ed25519",
        remedy=(
            "Remove the file, purge it from history, and issue a replacement key. The committed one "
            "must be treated as compromised: it was readable by everyone with repository access and "
            "remains in every clone and in the commits that carried it."
        ),
        remedy_path="(the reported file)",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "the fix is a history rewrite plus a key rotation. Both are irreversible and reach "
                "outside the working tree; performing them automatically could revoke access that "
                "something in production is currently using."
            ),
            partial_offer="the .gitignore entry and the list of paths that need purging and reissuing",
        ),
        artifact="",
    ),
    # ─── IaC ─────────────────────────────────────────────────────────────────
    "iac_sources_present": CheckExplanation(
        looked_for="infrastructure declared in code",
        looked_in="*.tf, *.tofu, terraform/*, tofu/*, pulumi.yaml, cloudformation/*",
        remedy=(
            "Declare the infrastructure this project needs in code and commit it. Infrastructure in "
            "code is reviewable before it exists and revertible after; a console click is neither, "
            "and leaves no record of what was intended."
        ),
        remedy_path="terraform/main.tf",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the module skeleton, provider block and resource definitions can be written, but "
                "region, account and sizing are choices the repository does not contain"
            ),
        ),
        artifact="opentofu",
    ),
    "iac_provider_lock_present": CheckExplanation(
        looked_for="a provider dependency lock",
        looked_in=".terraform.lock.hcl",
        remedy=(
            "Run `tofu init` (or `terraform init`) and commit the generated .terraform.lock.hcl. "
            "Without it the same plan can select a different provider version on a different day, "
            "so identical code can produce different infrastructure."
        ),
        remedy_path=".terraform.lock.hcl",
        fixability=Fixability(
            generatable=False,
            blocked_because=(
                "the lock records the provider versions and hashes that `init` resolved against the "
                "registry. Writing it by hand would assert hashes nothing verified."
            ),
            partial_offer="the `required_providers` block with explicit version constraints, and the init command",
        ),
        artifact="",
    ),
    "iac_remote_state_configured": CheckExplanation(
        looked_for="a backend block, so state is stored remotely and can be locked",
        looked_in="the backend blocks of every .tf or .tofu file that was found",
        remedy=(
            "Add a backend to the terraform block and name a bucket that supports locking:\n\n"
            "    terraform {\n"
            '      backend "s3" {\n'
            '        bucket         = "<your-state-bucket>"\n'
            '        key            = "<project>/terraform.tfstate"\n'
            '        region         = "<region>"\n'
            "        use_lockfile   = true\n"
            "      }\n"
            "    }\n\n"
            "Local state lives on one machine and cannot be locked, so two people applying at once "
            "silently overwrite each other and the file that says what exists is on somebody's laptop."
        ),
        remedy_path="terraform/backend.tf",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the block can be written with the bucket, key and region left as named placeholders "
                "for you to fill, because the state bucket is infrastructure that has to exist before "
                "the block can point at it — and creating one automatically would provision a billable "
                "resource in an account this never chose"
            ),
        ),
        artifact="opentofu",
    ),
    # ─── Cross-category consistency ──────────────────────────────────────────
    #
    # Every entry above judges one artifact. These three judge two against each other, which is where
    # the faults that reach production live: each individual file reviews cleanly and the pair describes
    # two different pieces of software.
    "ci_builds_an_existing_dockerfile": CheckExplanation(
        looked_for="a Dockerfile for every container build a workflow performs",
        looked_in="the run: and uses: steps of every workflow, matched against the indexed paths",
        remedy=(
            "Either add the Dockerfile the workflow builds, or correct the path the build step names. "
            "A `-f docker/api.Dockerfile` that does not exist fails the job on every run, and a build "
            "with no Dockefile at all fails before it starts.\n\n"
            "Check the -f/--file argument against the repository: a root Dockerfile does not satisfy a "
            "build that asks for one in a subdirectory."
        ),
        remedy_path="(the Dockerfile the workflow names)",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the missing Dockerfile can be written at the path the workflow already names, so the "
                "two agree without the workflow changing"
            ),
        ),
        artifact="dockerfile",
    ),
    "kubernetes_images_are_built_here": CheckExplanation(
        looked_for="every image a manifest deploys to be named by a workflow in this repository",
        looked_in="the image: values of every container, matched against the text of every workflow",
        remedy=(
            "Make the manifest and the pipeline name the same image. Either add a build-and-push step "
            "for the image the manifest deploys, or point the manifest at the image the pipeline "
            "already pushes.\n\n"
            "Until they agree, a deploy applies whatever happens to be in the registry under that "
            "name, which nothing in this repository produced or reviewed."
        ),
        remedy_path="k8s/deployment.yaml or .github/workflows/ci.yml",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the push step or the corrected image reference can be written, but which of the two is "
                "wrong is a decision about where the image is meant to come from"
            ),
        ),
        artifact="github_workflow",
    ),
    "env_example_matches_the_deployment": CheckExplanation(
        looked_for=(
            "the committed example to name exactly the variables the compose file and the container specs pass"
        ),
        looked_in="the environment: and env: blocks of every compose service and container spec",
        remedy=(
            "Add the missing names to the example with an empty value and a comment saying what each "
            "is for. If a name in the example is passed by nothing, either add it to the deployment or "
            "remove it.\n\n"
            "An example that exists and is incomplete is worse than none, because it reads as a "
            "complete list: somebody follows it exactly and gets a service that cannot start, with "
            "nothing to tell them what is absent."
        ),
        remedy_path=".env.example",
        fixability=Fixability(
            generatable=True,
            partial_offer=(
                "the missing names can be added with placeholders, because both sides of the comparison "
                "are already in the repository; what each variable should default to is not"
            ),
        ),
        artifact="env_example",
    ),
}
