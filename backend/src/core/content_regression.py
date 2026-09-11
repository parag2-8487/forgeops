# SPDX-License-Identifier: FSL-1.1-ALv2
"""A modification may not remove a property the file it replaces already satisfied.

THE FAILURE THIS EXISTS FOR. A user scored a project at 90, followed the recommendation the product
itself printed, generated, approved, and scored again: **85**. The drop was honest. Generation had
replaced a 1301-byte `k8s/deployment.yaml` — pinned image, CPU and memory requests and limits,
liveness and readiness probes, `runAsNonRoot` — with a 345-byte one that had none of them:

    image: test-3:latest        (was demo-service:1.0.0)
    no resources                (was limits and requests for cpu and memory)
    no probes                   (was livenessProbe and readinessProbe)
    no securityContext          (was runAsNonRoot, runAsUser 10001)

Three readiness checks flipped from pass to fail and `orchestration` fell from 275/275 to 195/275.
The scoring was the only part of that sequence which behaved correctly.

WHY THE EXISTING GATE DID NOT CATCH IT. §11.5.5's gate asks "is this document well formed and the
right shape". The 345-byte Deployment is valid YAML and a legal Deployment. It is not malformed; it
is *worse*, and "worse than what is already on disk" was a question nothing asked.

WHY THIS IS A GATE AND NOT A BETTER PROMPT. The prompt is also improved, and it will still sometimes
be ignored — D-105 measured a model complying with everything it was told and still omitting the one
thing that mattered. A prompt changes the odds; a gate changes the outcome. This is the half that
makes "the score cannot go down because of an approved change set" a property rather than a hope.

HOW IT REPORTS. Findings use the same `"{path}: ..."` prefix the rest of the gate uses, so the
existing per-file machinery (D-106) applies unchanged: while an attempt remains the model is told what
it dropped and can put it back, and on the last attempt the offending artifact alone is withheld while
its siblings are delivered.

WHAT IT DELIBERATELY DOES NOT DO. It does not require an artifact to IMPROVE anything, and it does not
judge a `create` — a file that did not exist cannot have lost a property. It only refuses to trade a
satisfied property for an unsatisfied one, which is the narrowest rule that makes the score monotone
under approval.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from .content_validation import (
    dockerfile_has_no_baked_secrets,
    kubernetes_containers_are_unprivileged,
    kubernetes_manifests_are_well_formed,
)
from .manifest_facts import (
    basename,
    dockerfile_base_pinned,
    dockerfile_healthcheck,
    kubernetes_image_tags_pinned,
    kubernetes_probes,
    kubernetes_resource_limits,
)

#: A guard answers "does this text satisfy the property", given the path and the text.
#:
#: Normalised to one shape here because the underlying validators come in two: the Kubernetes facts
#: take `(paths, contents)` so they can reason across a manifest set, and the Dockerfile ones take a
#: single body. Adapting at the edge keeps the table below readable and leaves both sets of callers
#: untouched.
_Guard = Callable[[str, str], bool]


def _manifest(fact: Callable[[tuple[str, ...], Mapping[str, str]], tuple[bool, str]]) -> _Guard:
    return lambda path, text: bool(fact((path,), {path: text})[0])


def _dockerfile_bool(fact: Callable[[str], bool]) -> _Guard:
    return lambda path, text: bool(basename(path).lower().startswith("dockerfile")) and fact(text)


def _dockerfile_pair(fact: Callable[[str], tuple[bool, str]]) -> _Guard:
    return lambda path, text: bool(basename(path).lower().startswith("dockerfile")) and bool(fact(text)[0])


#: Each property a modification may not silently drop, with the sentence a user is shown.
#:
#: KEYED BY THE READINESS CHECK ID where one exists, so a person can line the refusal up against the
#: line on the readiness screen that would have moved. The message says what would be LOST, not what
#: is missing: "no liveness probe" reads as a pre-existing gap, and this is a regression.
GUARDED_PROPERTIES: Final[tuple[tuple[str, _Guard, str], ...]] = (
    (
        "kubernetes_resource_limits_declared",
        _manifest(kubernetes_resource_limits),
        "the CPU and memory requests and limits its containers currently declare",
    ),
    (
        "kubernetes_probes_declared",
        _manifest(kubernetes_probes),
        "the liveness and readiness probes its containers currently declare",
    ),
    (
        "kubernetes_image_tags_pinned",
        _manifest(kubernetes_image_tags_pinned),
        "the pinned image tag it currently uses, replacing it with `latest` or none",
    ),
    (
        "kubernetes_containers_unprivileged",
        _manifest(kubernetes_containers_are_unprivileged),
        "the absence of privileged containers — the replacement adds one",
    ),
    (
        "kubernetes_manifests_well_formed",
        _manifest(kubernetes_manifests_are_well_formed),
        "a manifest set that parses and carries the fields a Kubernetes object needs",
    ),
    (
        "dockerfile_no_baked_secrets",
        _dockerfile_pair(dockerfile_has_no_baked_secrets),
        "the absence of a hard-coded credential — the replacement introduces one",
    ),
    (
        "dockerfile_healthcheck_present",
        _dockerfile_bool(dockerfile_healthcheck),
        "the HEALTHCHECK instruction it currently declares",
    ),
    (
        "dockerfile_base_image_pinned",
        _dockerfile_bool(dockerfile_base_pinned),
        "the pinned base image it currently uses",
    ),
)


def properties_lost(path: str, before: str, after: str) -> tuple[tuple[str, str], ...]:
    """Which guarded properties `before` satisfies and `after` does not.

    Returns `(check_id, description)` pairs. Empty when nothing regresses, which includes the case
    where the property was already unsatisfied — this refuses a trade, not a pre-existing gap.

    A property is only judged when the BEFORE text satisfies it. That ordering is the whole rule: a
    file that never declared probes is not made worse by a replacement that also does not, and
    demanding otherwise would turn every modification into a full remediation.
    """
    lost: list[tuple[str, str]] = []
    for check_id, guard, description in GUARDED_PROPERTIES:
        try:
            if not guard(path, before):
                continue
            still = guard(path, after)
        except Exception:  # noqa: BLE001 - an unparseable side is not evidence of a regression
            # A fact that cannot read either version tells us nothing. Refusing on that basis would
            # block a correct edit whenever a validator met a shape it did not expect, which is a
            # worse failure than missing one regression.
            continue
        if not still:
            lost.append((check_id, description))
    return tuple(lost)


def regression_findings(
    files: object,
    existing: Mapping[str, str] | None,
) -> tuple[str, ...]:
    """Gate findings for every artifact that would degrade the file it replaces.

    `files` is any iterable of objects carrying `.path` and `.content`; typed loosely so this module
    stays on `core` and does not import `generation`, which §2.2.1 keeps on the other side of the
    domain boundary.
    """
    if not existing:
        return ()

    findings: list[str] = []
    for artifact in files:  # type: ignore[union-attr]
        path = getattr(artifact, "path", "")
        content = getattr(artifact, "content", "")
        before = existing.get(path)
        if not path or before is None or not before.strip():
            # No stored text means this is a create, or a file the scan never read. Either way there
            # is no established property to lose, and inventing one from an empty string would
            # refuse every new file.
            continue
        for check_id, description in properties_lost(path, before, content):
            findings.append(
                f"{path}: this rewrite would remove {description}. "
                f"The file on disk satisfies `{check_id}` and the replacement does not, so approving "
                f"it would lower the readiness score. Modify the existing file instead of replacing "
                f"it: keep every block it already has and change only what the instruction named."
            )
    return tuple(findings)


#: What to tell a model to keep, per guarded property, phrased as an instruction rather than a label.
#:
#: THE OTHER HALF OF THE GATE. `regression_findings` refuses a rewrite that drops one of these; this
#: tells the model not to drop it in the first place. Without it the guard alone produces a stalemate:
#: the model keeps emitting the same wholesale rewrite, the gate keeps withholding it, and the checks
#: it was supposed to fix stay exactly where they were. The score stops falling and never rises.
#:
#: `_preserve_notes` in the prompt compiler already protected comment blocks and named build stages -
#: the two things a regeneration visibly destroys. It said nothing about the SEMANTIC properties the
#: readiness score actually measures, which is what the reported regression dropped.
PRESERVE_INSTRUCTIONS: Final[Mapping[str, str]] = {
    "kubernetes_resource_limits_declared": (
        "keep the existing `resources:` block on every container, with both `limits` and `requests` for cpu and memory"
    ),
    "kubernetes_probes_declared": "keep the existing `livenessProbe` and `readinessProbe` on every container",
    "kubernetes_image_tags_pinned": "keep the existing image tag exactly as it is; do not change it to `latest`",
    "kubernetes_containers_unprivileged": "keep every container unprivileged; do not add `privileged: true`",
    "kubernetes_manifests_well_formed": "keep every field the existing Kubernetes objects declare",
    "dockerfile_no_baked_secrets": "do not introduce any hard-coded credential",
    "dockerfile_healthcheck_present": "keep the existing `HEALTHCHECK` instruction",
    "dockerfile_base_image_pinned": "keep the base image pinned exactly as it is",
}


def properties_to_preserve(path: str, body: str) -> tuple[str, ...]:
    """Instructions for every guarded property `body` already satisfies.

    Read off the file rather than assumed, so a Deployment with no probes is not told to keep probes
    it does not have — which would be an instruction to invent something, and the compiler's whole
    contract is that it states facts.
    """
    if not body.strip():
        return ()
    notes: list[str] = []
    for check_id, guard, _description in GUARDED_PROPERTIES:
        try:
            if guard(path, body):
                instruction = PRESERVE_INSTRUCTIONS.get(check_id)
                if instruction:
                    notes.append(instruction)
        except Exception:  # noqa: BLE001 - a validator that cannot read the file has nothing to assert
            continue
    return tuple(notes)
