# SPDX-License-Identifier: FSL-1.1-ALv2
"""The artifact kinds FR-24 requires beyond a Dockerfile and Kubernetes manifests.

FR-24 is P0 and names four things: containerisation, Kubernetes manifests, **CI/CD pipelines**, and
**infrastructure as code**. Only the first two were produced. The consequence was not cosmetic — a user
asking the platform to make their project deployable got a Dockerfile and three manifests and nothing
that would build the image, and nothing that would create the cluster the manifests need.

The agent's `validate.yaml`, `validate.helm` and `validate.tofu` operations existed to check these and
had nothing to check. `artifact_checks` gates them in the backend too, so an unrunnable workflow or a
chart Helm will reject cannot reach a user.

WHY THESE ARE TEMPLATES AND NOT MODEL OUTPUT
--------------------------------------------
This module is the safe fallback — what a user gets when no model could be reached, or when three
attempts all failed the gate. The model path is asked for the same paths through `REQUIRED_ARTIFACTS`,
so a provider that produces good ones is preferred; these are what makes the platform still useful when
it does not. Every one is deliberately minimal and correct rather than comprehensive: a fallback that
guesses at a cloud provider's resource names would produce something worse than a small, valid module a
user can extend.
"""

from __future__ import annotations

from typing import Final

#: The tag every generated artifact references. One constant because three artifacts must agree: the
#: Deployment's `image:`, the chart's `image.tag`, and the tag the workflow builds. When they drifted,
#: `kubernetes_images_are_built_here` reported a manifest deploying something CI never produced.
#:
#: NOT `latest`. `kubernetes_image_tags_pinned` scores this, and the reason it does is that two applies
#: of one manifest must deploy the same code. `0.1.0` matches the chart's `appVersion`.
GENERATED_IMAGE_TAG: Final[str] = "0.1.0"

#: The uid generated workloads run as. Any high-numbered non-zero uid satisfies `runAsNonRoot`; this one
#: matches the `USER` line in the generated Dockerfile so the image and the manifest agree.
GENERATED_RUN_AS_USER: Final[int] = 1001

#: `actions/checkout` pinned to the commit this repository already vetted for its own workflows.
#:
#: A SHA, not a tag, because `pipeline_actions_pinned` requires exactly 40 lowercase hex characters and
#: the supply-chain reason behind that rule applies to the user's pipeline as much as to ours. This
#: value is not invented: it is the same pin `.github/workflows/` uses, so there is one place to update.
CHECKOUT_ACTION: Final[str] = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2"

#: Test command per runtime, keyed by the runtime `_render` detects. `pipeline_runs_tests` matches the
#: step's COMMAND, so a job merely named "test" does not count — and rightly, since a pipeline with no
#: tests to run reports green for every change.
_TEST_COMMANDS: Final[dict[str, str]] = {
    "node": "npm test --if-present",
    "python": "python -m pytest -q",
}


def github_workflow_yaml(
    app_name: str,
    *,
    image_tag: str = GENERATED_IMAGE_TAG,
    runtime: str = "python",
) -> str:
    """A workflow that builds the image the manifests reference, and runs the project's tests.

    TWO EARLIER DECISIONS HERE WERE WRONG AND ARE REVERSED.

    The first: this docstring used to say pinned action SHAs were "deliberately ABSENT ... the one place
    this repository's own rule is knowingly inverted", on the reasoning that a SHA the user cannot verify
    is worse than a tag they can read. But `pipeline_actions_pinned` is a check this platform scores the
    user against, worth 15 points, and `CHECK_EXPLANATIONS` names `github_workflow` as the artifact whose
    generation fixes it. So the generator emitted a file that failed the check the generator offered to
    fix. Whatever the merits of the tag argument, that combination cannot be defended: either pin, or
    stop claiming the artifact fixes the check. Pinning is the better half, and the SHA used is one this
    repository already relies on rather than a value conjured for the occasion.

    The second: there was no test step at all, so `pipeline_runs_tests` (30 points) failed for the same
    reason. The command is chosen from the runtime `_render` already detected for the Dockerfile, so the
    workflow tests the language the image actually contains.

    THIRD-PARTY DOCKER ACTIONS ARE GONE, and that is what makes the pinning honest rather than
    performative. `docker/setup-buildx-action` and `docker/build-push-action` would each need a SHA, and
    this repository has vetted neither — writing two SHAs I cannot stand behind to satisfy a checker
    would be exactly the fabrication the rule exists to prevent. `docker build` via `run:` needs no
    action, is pinned by definition, and builds the same image.
    """
    test_command = _TEST_COMMANDS.get(runtime, _TEST_COMMANDS["python"])
    return f"""---
name: build

# `on` is quoted because YAML 1.1 reads a bare `on` as the boolean true. Unquoted it still works in
# GitHub Actions, but every linter reports it, so the generated file avoids the argument.
"on":
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read
  packages: write

concurrency:
  group: build-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      # Pinned to a commit SHA rather than a tag: a tag is mutable, so what runs here could change
      # without this file changing. Update the SHA and the trailing version comment together.
      - uses: {CHECKOUT_ACTION}

      # Tests run BEFORE the build. A pipeline that publishes an image and then discovers the tests
      # fail has already shipped the defect.
      - name: Run the tests
        run: {test_command}

      # `docker build` rather than a build-push action: no third-party action means nothing to pin and
      # nothing to trust. The tag matches the manifests and the chart so CI produces what they deploy.
      - name: Build the image
        run: docker build -f Dockerfile -t {app_name}:{image_tag} .
"""


def helm_chart_yaml(app_name: str) -> str:
    """`Chart.yaml`. `version` is SemVer 2 because Helm requires it rather than prefers it."""
    return f"""---
apiVersion: v2
name: {app_name}
description: A Helm chart for {app_name}, generated by ForgeOps.
type: application
# Helm REQUIRES SemVer 2 here and refuses the chart otherwise.
version: 0.1.0
appVersion: "0.1.0"
"""


def helm_values_yaml(app_name: str, port: int) -> str:
    """The chart's values.

    `resources` USED TO BE `{}` with a comment arguing that a guessed request is worse than none. The
    argument has real merit and is still wrong here, for the same reason the Deployment template's was:
    `kubernetes_resource_limits_declared` is a check this platform scores the user against, so an
    artifact that leaves the block empty hands the user a failing check the artifact existed to fix. A
    modest labelled default they will tune beats both an empty block and a silent failure.

    `tag` is the shared constant rather than `latest`, so the chart, the manifests and the workflow all
    name one image.
    """
    return f"""---
replicaCount: 1

image:
  repository: {app_name}
  # An explicit tag, not `latest`: two installs of one chart must deploy the same code.
  tag: "{GENERATED_IMAGE_TAG}"
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: {port}

# A starting point, not a measurement. Requests are what the scheduler reserves; limits are what stop
# this container taking the node. Tune both once the workload's real usage is known.
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"

# Probe path `/` because nothing here knows the application serves a dedicated health route, and a probe
# pointed at a 404 restarts a healthy container forever.
probes:
  path: /

securityContext:
  runAsNonRoot: true
  runAsUser: {GENERATED_RUN_AS_USER}
  allowPrivilegeEscalation: false
"""


def helm_deployment_template(app_name: str) -> str:
    """The chart's one template. Rendered by `helm template`, so it must actually render."""
    return f"""---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{{{ include "{app_name}.fullname" . }}}}
  labels:
    app.kubernetes.io/name: {app_name}
spec:
  replicas: {{{{ .Values.replicaCount }}}}
  selector:
    matchLabels:
      app.kubernetes.io/name: {app_name}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {app_name}
    spec:
      securityContext:
        runAsNonRoot: {{{{ .Values.securityContext.runAsNonRoot }}}}
        runAsUser: {{{{ .Values.securityContext.runAsUser }}}}
      containers:
        - name: {app_name}
          image: "{{{{ .Values.image.repository }}}}:{{{{ .Values.image.tag }}}}"
          imagePullPolicy: {{{{ .Values.image.pullPolicy }}}}
          ports:
            - containerPort: {{{{ .Values.service.port }}}}
          livenessProbe:
            httpGet:
              path: {{{{ .Values.probes.path }}}}
              port: {{{{ .Values.service.port }}}}
            initialDelaySeconds: 10
            periodSeconds: 20
          readinessProbe:
            httpGet:
              path: {{{{ .Values.probes.path }}}}
              port: {{{{ .Values.service.port }}}}
            initialDelaySeconds: 5
            periodSeconds: 10
          securityContext:
            allowPrivilegeEscalation: {{{{ .Values.securityContext.allowPrivilegeEscalation }}}}
          resources: {{{{- toYaml .Values.resources | nindent 12 }}}}
"""


def helm_helpers_template(app_name: str) -> str:
    """`_helpers.tpl`, because the deployment template calls `include`.

    Without this the chart lints and fails to render, which is precisely the case `helm template`
    catches and `helm lint` does not.
    """
    return f"""{{{{- define "{app_name}.fullname" -}}}}
{{{{- printf "%s-%s" .Release.Name "{app_name}" | trunc 63 | trimSuffix "-" -}}}}
{{{{- end -}}}}
"""


def opentofu_main_tf(app_name: str, port: int) -> str:
    """A module that validates and plans, with remote state declared as a partial configuration.

    The `kubernetes` provider rather than a cloud one, deliberately: the manifests this accompanies are
    Kubernetes, so the infrastructure that matches them is a namespace and a deployment. Generating an
    AWS VPC for a project whose target is unknown would be a guess with a bill attached.

    REMOTE STATE USED TO BE ABSENT, and the docstring said so approvingly — "no credentials and no
    remote state". `iac_remote_state_configured` is worth 25 points and names `opentofu` as the artifact
    that fixes it, so once again the generator shipped a file failing the check it claimed to fix. It is
    also the most consequential of the three: local state means the first colleague to run `apply`
    cannot see what the first one created, and concurrent applies corrupt each other.

    The block is a PARTIAL configuration — a backend type with no bucket, key or region. That is a real
    OpenTofu idiom, not a placeholder: `tofu init -backend-config=...` supplies the rest, and `tofu
    validate` accepts it as written. Hard-coding a bucket name would be inventing infrastructure that
    does not exist, which is worse than declaring the intent and letting init bind it.
    """
    return f"""terraform {{
  required_version = ">= 1.6.0"

  # Remote state, as a PARTIAL configuration: the type is declared here, and the bucket, key and region
  # are supplied at init time with `tofu init -backend-config=backend.hcl`. Local state means a
  # colleague's `apply` cannot see what yours created, and two concurrent applies corrupt each other.
  #
  # Swap `s3` for `gcs`, `azurerm` or any other supported backend — what matters is that state is not
  # sitting in one working copy.
  backend "s3" {{}}

  required_providers {{
    kubernetes = {{
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }}
  }}
}}

variable "namespace" {{
  description = "Namespace to deploy {app_name} into."
  type        = string
  default     = "{app_name}"
}}

variable "image" {{
  description = "Fully qualified image reference for {app_name}."
  type        = string
  default     = "{app_name}:{GENERATED_IMAGE_TAG}"
}}

variable "replicas" {{
  description = "Number of pod replicas."
  type        = number
  default     = 1
}}

variable "container_port" {{
  description = "Port the container listens on."
  type        = number
  default     = {port}
}}

resource "kubernetes_namespace" "app" {{
  metadata {{
    name = var.namespace
  }}
}}

resource "kubernetes_deployment" "app" {{
  metadata {{
    name      = "{app_name}"
    namespace = kubernetes_namespace.app.metadata[0].name
  }}

  spec {{
    replicas = var.replicas

    selector {{
      match_labels = {{
        "app.kubernetes.io/name" = "{app_name}"
      }}
    }}

    template {{
      metadata {{
        labels = {{
          "app.kubernetes.io/name" = "{app_name}"
        }}
      }}

      spec {{
        security_context {{
          run_as_non_root = true
          run_as_user     = 1001
        }}

        container {{
          name  = "{app_name}"
          image = var.image

          port {{
            container_port = var.container_port
          }}

          security_context {{
            allow_privilege_escalation = false
          }}
        }}
      }}
    }}
  }}
}}

output "namespace" {{
  description = "Namespace {app_name} was deployed into."
  value       = kubernetes_namespace.app.metadata[0].name
}}
"""
