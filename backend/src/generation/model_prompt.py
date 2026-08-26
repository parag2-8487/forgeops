# SPDX-License-Identifier: FSL-1.1-ALv2
"""The prompt a generation run sends to a model, and the parse of what comes back.

WHY THIS MODULE EXISTS
----------------------
`GenerationService` had no model call at all. It rendered four artifacts from string
concatenation and streamed them in 120-character slices, and its own docstring said so:
"the Phase 1 template path ... not a live model call", "Not real tokenisation, and named so".
`generation_runs.served_from` was the SQL literal `'template'` in the INSERT, so the column
could not have recorded anything else even if a model had been called.

Routing to a real model needs two things the template path never needed: a prompt that states
the contract precisely enough that a small local model can satisfy it, and a parse that turns
free-form model output back into `(path, content)` pairs. Both are here rather than in
`service.py` so the streaming state machine stays readable and so each can be tested against
fixture text without a model.

WHY THE FORMAT IS THIS STRICT
-----------------------------
The `self_hosted` tier's whole point is a model small enough to run on a developer machine, and
a 1.5B-class model does not reliably produce well-formed JSON of any depth. A line-oriented
marker followed by a fenced block is the form such models emit most reliably, and — more
importantly — it is the form whose FAILURES are detectable: a missing marker is a missing file,
not a plausible-looking object with a silently empty member. An artifact set that does not parse
is treated as a failed provider attempt and retried, which is exactly what §11.5's bounded
feedback loop is for.

WHAT IS NOT HERE
----------------
No default content, no partial fill-in, no "if the model omitted the Service, synthesise one".
A parse either yields every required artifact or reports what is missing. Filling a gap with a
template while `served_from` said `provider` is precisely the fabrication this work removes; the
template path is a separate, LABELLED outcome in `service.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The artifacts §12.6 step 6 names. Ordered, because the change set the governance chokepoint
#: receives is built from this sequence and a stable order makes two runs comparable.
REQUIRED_ARTIFACTS: tuple[str, ...] = (
    "Dockerfile",
    "k8s/deployment.yaml",
    "k8s/service.yaml",
    "k8s/ingress.yaml",
)

#: `### FILE: <path>`, tolerating any number of leading hashes and surrounding whitespace.
#:
#: Deliberately lenient about the marker and strict about the path. Models vary the heading
#: level freely and that variance carries no information, whereas a path this does not recognise
#: is a file that will be reported missing — so leniency where it costs nothing, and no guessing
#: where it would matter.
_FILE_MARKER = re.compile(r"^\s*#{0,6}\s*FILE:\s*[`\"']?([^`\"'\s]+)[`\"']?\s*$", re.IGNORECASE)

#: A fenced code block delimiter, with or without a language tag.
_FENCE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$")


class ArtifactParseError(ValueError):
    """The model's output did not contain the artifact set the contract asks for.

    Carries `missing` so `service.py` can put the specific gap into the retry prompt. A bare
    "parse failed" would make the second attempt identical to the first, which turns a bounded
    feedback loop into three copies of the same request.
    """

    def __init__(self, *, missing: Sequence[str], found: Sequence[str]) -> None:
        self.missing = tuple(missing)
        self.found = tuple(found)
        super().__init__(f"model output is missing {list(self.missing)}; it contained {list(self.found)}")


@dataclass(frozen=True, slots=True)
class ProjectFacts:
    """What the prompt is allowed to assert about the application.

    A dataclass rather than the raw `projects` row, because the prompt must not become a place
    where arbitrary operator-supplied settings are interpolated into model input. Every member
    here is read by `service.py` from the row and named, so adding a fact is a deliberate act.
    """

    app_name: str
    runtime: str
    port: int
    base_image: str
    start_command: tuple[str, ...]


def build_generation_prompt(
    *,
    operator_prompt: str,
    facts: ProjectFacts,
    previous_findings: Sequence[str] = (),
    attempt: int = 1,
) -> str:
    """The full model prompt for one attempt.

    `previous_findings` are the deterministic gate's verdict on the PREVIOUS attempt, quoted back
    verbatim. That is what makes retry #2 a repair rather than a repeat, and it is also what
    makes the retry reach the provider at all: the cache key is computed over the prompt, so an
    identical retry prompt would be served from L1 with the same rejected content and the loop
    would spend all three attempts on one bad answer.

    `attempt` is in the prompt for the same reason, one step further along. Findings are often
    IDENTICAL across retries — a model that ignored `USER 1001` once frequently ignores it twice —
    so quoting the findings alone leaves attempts 2 and 3 with the same prompt, the same cache key,
    and an L1 hit that serves the rejected content without the provider ever being asked again.
    `test_the_retry_prompt_differs_so_the_cache_cannot_serve_the_rejection` caught exactly that:
    three attempts, two provider calls. The attempt number is also a true statement about the
    request rather than a nonce added to defeat the cache.
    """
    start = " ".join(facts.start_command) if facts.start_command else ""
    lines = [
        "You are a DevOps engineer. Produce deployment artifacts for the application described below.",
        "",
        "APPLICATION FACTS (these are authoritative; do not contradict them):",
        f"- name: {facts.app_name}",
        f"- runtime: {facts.runtime}",
        f"- container base image: {facts.base_image}",
        f"- listening port: {facts.port}",
    ]
    if start:
        lines.append(f"- start command: {start}")
    lines += [
        "",
        "OPERATOR REQUEST:",
        operator_prompt.strip(),
        "",
        "HARD REQUIREMENTS. These are checked mechanically and output that breaks any of them is",
        "rejected automatically, so follow them literally:",
        f"- The Dockerfile's FIRST LINE is exactly `FROM {facts.base_image}`. Do not put a comment,",
        "  a blank line or anything else above it.",
        "- The Dockerfile contains a line that is exactly `USER 1001`, placed after the RUN",
        "  instructions and before CMD, so the container does not run as root.",
        f"- The Dockerfile EXPOSEs port {facts.port}.",
        "- Every Kubernetes manifest has top-level `apiVersion:`, `kind:`, `metadata:` and `spec:` keys.",
        f"- The Deployment labels its pods `app: {facts.app_name}` and the Service selects on that label.",
        f"- The Service is named `{facts.app_name}`, is type ClusterIP, publishes port 80 and targets",
        f"  containerPort {facts.port}.",
        f"- The Ingress backend names service `{facts.app_name}` on port number 80.",
        f"- Use the real name `{facts.app_name}` everywhere. Never emit a placeholder such as",
        "  `<your-username>` or `example.com`: these files are applied as written.",
        "",
        "OUTPUT FORMAT. Emit exactly these four files, in this order, and nothing else -",
        "no explanation, no summary, no extra files:",
    ]
    for path in REQUIRED_ARTIFACTS:
        lines.append(f"### FILE: {path}")
        lines.append("```")
        lines.append("<the file's complete contents>")
        lines.append("```")

    if previous_findings:
        lines += [
            "",
            f"ATTEMPT {attempt}. YOUR PREVIOUS ATTEMPT WAS REJECTED. Fix exactly these findings and",
            "re-emit all four files:",
        ]
        lines += [f"- {finding}" for finding in previous_findings]

    return "\n".join(lines)


def parse_artifacts(raw: str, *, required: Sequence[str] = REQUIRED_ARTIFACTS) -> dict[str, str]:
    """Split model output into `{path: content}`, or raise `ArtifactParseError`.

    Content is taken verbatim between the fences. Nothing is normalised except the removal of the
    fence lines themselves and a trailing newline guarantee, because a Dockerfile or a manifest is
    whitespace-significant and "helpfully" reformatting model output would mean the bytes the
    deterministic gate judged are not the bytes that get written.
    """
    files: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False

    for line in raw.splitlines():
        marker = _FILE_MARKER.match(line)
        if marker and not in_fence:
            current = marker.group(1).strip().lstrip("./")
            files.setdefault(current, [])
            continue
        if current is None:
            # Preamble the model emitted before the first marker. Discarded rather than treated as
            # an error: a chatty model is not a broken one, and the contract is about what IS
            # present, not about what precedes it.
            continue
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            files[current].append(line)

    contents = {path: _join(body) for path, body in files.items() if _join(body).strip()}
    missing = [path for path in required if path not in contents]
    if missing:
        raise ArtifactParseError(missing=missing, found=sorted(contents))
    # Only the required set is returned. A model that invented `README.md` gets it dropped here
    # rather than at the governance chokepoint, because an unrequested file in a change set is a
    # write nobody asked for.
    return {path: contents[path] for path in required}


def _join(body: Sequence[str]) -> str:
    """Re-join a captured block, guaranteeing the trailing newline a text file should have."""
    if not body:
        return ""
    text = "\n".join(body)
    return text if text.endswith("\n") else text + "\n"


def facts_from_project(
    *,
    project: Mapping[str, Any] | None,
    operator_prompt: str,
    default_app_name: str,
) -> ProjectFacts:
    """Derive the prompt's authoritative facts from the `projects` row.

    Deliberately the SAME derivation `GenerationService._render` applies for the template path,
    so a model run and a template run describe the same application. If the two disagreed, a
    template fallback would silently produce different infrastructure than the provider attempt it
    replaced — the operator would see the port change and have no way to know why.
    """
    settings: Mapping[str, Any] = {}
    if project is not None:
        raw_settings = project.get("settings")
        if isinstance(raw_settings, Mapping):
            settings = raw_settings

    runtime = str(settings.get("runtime") or "").strip().lower()
    if not runtime:
        lowered = operator_prompt.lower()
        runtime = "node" if ("node" in lowered or "express" in lowered) else "python"

    if runtime.startswith("node"):
        base_image, start, port = "node:20-alpine", ("node", "server.js"), 3000
    else:
        base_image, start, port = "python:3.11-slim", ("python", "main.py"), 8000

    if str(settings.get("base_image") or "").strip():
        base_image = str(settings["base_image"]).strip()
    try:
        if settings.get("port") is not None:
            configured = int(settings["port"])
            if 1 <= configured <= 65535:
                port = configured
    except (TypeError, ValueError):
        # A non-numeric port in operator-entered settings is not a reason to fail a run; the
        # runtime default is a better answer than a stack trace.
        pass
    configured_start = settings.get("start_command")
    if isinstance(configured_start, list | tuple) and configured_start:
        start = tuple(str(part) for part in configured_start)
    elif isinstance(configured_start, str) and configured_start.strip():
        start = tuple(configured_start.split())

    return ProjectFacts(
        app_name=default_app_name,
        runtime=runtime,
        port=port,
        base_image=base_image,
        start_command=start,
    )
