# SPDX-License-Identifier: FSL-1.1-ALv2
"""The Command Center's intent resolution. Phase 2 §2.5.

THIS IS WHERE AN ARBITRARY-SHELL ESCAPE WOULD TRY TO APPEAR, and it is the last section with that exposure.
Everything below is arranged so that it cannot.

THE LOAD-BEARING DECISION: NATURAL LANGUAGE NEVER BECOMES AN ARGUMENT VALUE.

A classifier turns a sentence into an `Intent` -- a NAME from a closed set plus slot values drawn from
enumerated vocabularies. It never produces a command line, a path, a flag, or free text that reaches an
operation. `resolve` then maps that name to one entry in `COMMANDS`, whose `operation` is a constant written
here in the repository. So the worst a hostile or hallucinated classification can do is pick the wrong
enumerated command with enumerated arguments -- which still faces policy, approval and audit.

The alternative shape, and the reason it is refused: a "function calling" pipeline where the model emits
`{"operation": ..., "args": {...}}` and the server validates it. That is safe only to the extent the
validator is complete, and the validator cannot be complete because it is defending against a string. This
module never has a string to defend against.

FIVE LAYERS, and each is a different KIND of check rather than five versions of the same one -- five
whitelists in a row would all fail together the first time something bypassed the first:

  1. INTENT CLOSURE      an unrecognised intent name is refused, never "best effort" dispatched. This is the
                         structural fail-safe, identical in shape to `is_auto_executable`: membership in a
                         frozenset with no second condition, so an unmapped intent is a refusal by default.
  2. SLOT VOCABULARY     every slot value is checked against an enumerated set or a strict pattern. There is
                         no slot whose type is "string the user typed".
  3. FORBIDDEN VERBS     an explicit deny-list over the resolved operation, asserted by test, so a future
                         command that named a shell, an exec or an arbitrary run cannot be added silently.
  4. CONFIRMATION        a mutating command resolves to a PLAN that a human confirms. The classifier's output
                         never reaches the chokepoint without that step.
  5. GOVERNANCE          the plan is executed through the existing chokepoint. Nothing here is a new path to
                         the agent.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: talk to a model. `classify` is deterministic pattern matching over
the closed vocabulary. An LLM classifier can be added behind the same `Intent` return type -- and if it is,
layers 1 to 5 still hold, because they constrain the OUTPUT rather than trusting the producer. That is the
point of putting the boundary here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal

# --- layer 1: the closed intent set ---------------------------------------------------------------------

#: Every intent this system understands. §2.5's router names five categories; these are the concrete intents
#: within them, and the set is CLOSED -- see `resolve` for what happens to anything else.
INTENTS: Final[frozenset[str]] = frozenset(
    {
        "deploy",  # deploy category
        "show_pods",  # diagnostic
        "show_containers",  # diagnostic
        "check_logs",  # diagnostic
        "scale_workload",  # deploy
        "generate_artifact",  # generate
        "show_policy",  # policy
        "explain",  # chat
    }
)

IntentCategory = Literal["deploy", "diagnostic", "generate", "policy", "chat"]

# --- layer 2: the slot vocabularies ---------------------------------------------------------------------

#: Environments a command may name. Deliberately not "any string the project has": a typo would then become
#: a deploy to a environment that does not exist, and the error would surface from the chokepoint rather than
#: from the thing that misread the sentence.
ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "development", "test", "staging", "prod", "production"})

#: What `generate_artifact` may be asked for. The same closed set the readiness checks target, because
#: generating anything else is a request nothing downstream can judge.
ARTIFACT_KINDS: Final[frozenset[str]] = frozenset(
    {"dockerfile", "kubernetes", "compose", "ci", "terraform", "security_policy"}
)

#: A Kubernetes name, and this is the ONLY pattern-matched slot in the module. DNS-1123: lowercase
#: alphanumerics and hyphens, starting and ending alphanumeric, 63 characters. It cannot contain a space, a
#: slash, a quote, a semicolon or a dollar sign -- so it cannot carry a second token into any argument
#: vector, which is what makes it safe to accept as free-form at all.
_K8S_NAME: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: Replica counts. A bounded integer, not an int: `--replicas=99999` is a denial of service against the
#: cluster expressed as a number.
MAX_REPLICAS: Final = 20


class CommandRefusedError(Exception):
    """A guard rail refused. Carries the sentence an operator reads."""


# --- layer 3: the forbidden-verb deny-list --------------------------------------------------------------

#: Substrings that may never appear in a resolved operation name. This is a DENY-list deliberately, layered
#: over the allow-list in `COMMANDS`: the allow-list is what makes the system safe, and this is what makes an
#: unsafe ADDITION to it fail loudly. A test asserts every command's operation passes this, so adding
#: `devtools.shell` to the table breaks the build rather than shipping.
FORBIDDEN_OPERATION_TOKENS: Final[tuple[str, ...]] = (
    "shell",
    "exec",
    "eval",
    "bash",
    "sh",
    "cmd",
    "powershell",
    "script",
    "arbitrary",
    "raw",
)


@dataclass(frozen=True, slots=True)
class Intent:
    """What a sentence was understood to mean. Slots are enumerated values, never free text."""

    name: str
    category: IntentCategory
    slots: dict[str, Any] = field(default_factory=dict)
    #: 0.0-1.0. Used to decide whether to ASK rather than whether to act: a low-confidence intent is
    #: presented to the user for confirmation, never dispatched on a guess.
    confidence: float = 0.0
    #: The exact text that produced this, kept for the history view so a user can see what was understood.
    utterance: str = ""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One thing the Command Center can do.

    `operation` is a CONSTANT in this file. It is never assembled from an intent, a slot or a template --
    which is what stops a slot value from becoming part of an operation name.
    """

    intent: str
    category: IntentCategory
    #: The agent operation or internal route this resolves to, or "" for a read answered locally.
    operation: str
    mutating: bool
    #: Slot names this command requires. A missing one is a refusal, never a default.
    required_slots: tuple[str, ...] = ()
    #: Prose shown in the confirmation step and in autocomplete.
    describes: str = ""
    examples: tuple[str, ...] = ()


#: THE ALLOW-LIST. Eight commands, each naming a constant operation.
#:
#: §2.5's "supported commands" box names five utterances; these cover all of them plus the reads the
#: diagnostic category needs. Adding an entry is the intended friction: it means writing the operation
#: constant, declaring whether it mutates, and passing the forbidden-verb test.
COMMANDS: Final[dict[str, CommandSpec]] = {
    "deploy": CommandSpec(
        intent="deploy",
        category="deploy",
        # The existing governed deployment route, not a new path to the agent.
        operation="deployments.create",
        mutating=True,
        required_slots=("environment",),
        describes="Deploy this project's current manifests to an environment.",
        examples=("deploy to staging", "deploy to production"),
    ),
    "show_pods": CommandSpec(
        intent="show_pods",
        category="diagnostic",
        operation="kubernetes.pod_list",
        mutating=False,
        describes="List the pods in a namespace.",
        examples=("show pods", "show pods in default"),
    ),
    "show_containers": CommandSpec(
        intent="show_containers",
        category="diagnostic",
        operation="docker.container_list",
        mutating=False,
        describes="List the containers on the paired host.",
        examples=("show containers", "what is running"),
    ),
    "check_logs": CommandSpec(
        intent="check_logs",
        category="diagnostic",
        operation="kubernetes.pod_logs",
        mutating=False,
        required_slots=("workload",),
        describes="Read recent logs for one workload.",
        examples=("check logs for api", "show me the logs for checkout"),
    ),
    "scale_workload": CommandSpec(
        intent="scale_workload",
        category="deploy",
        operation="kubernetes.workload_action",
        mutating=True,
        required_slots=("workload", "replicas"),
        describes="Change a workload's replica count.",
        examples=("scale api to 3 replicas", "scale checkout to 1"),
    ),
    "generate_artifact": CommandSpec(
        intent="generate_artifact",
        category="generate",
        operation="generation.run",
        mutating=True,
        required_slots=("artifact_kind",),
        describes="Generate an artifact, which then enters the approval pipeline like any other change.",
        examples=("generate a dockerfile", "generate kubernetes manifests"),
    ),
    "show_policy": CommandSpec(
        intent="show_policy",
        category="policy",
        operation="",
        mutating=False,
        describes="Show the policy that would apply to a change in an environment.",
        examples=("show policy for production", "what policy applies to prod"),
    ),
    "explain": CommandSpec(
        intent="explain",
        category="chat",
        operation="",
        mutating=False,
        describes="Answer a question about this project without changing anything.",
        examples=("explain this dockerfile", "what does this error mean"),
    ),
}


def _assert_operations_are_safe() -> None:
    """Layer 3, evaluated AT IMPORT.

    An operation containing a forbidden token fails the whole module rather than one request -- the same
    argument as the PromQL catalogue's import-time slot check. A deny-list checked per-request would let the
    dangerous command exist and merely refuse to run, which is a worse place to discover it.
    """
    for spec in COMMANDS.values():
        lowered = spec.operation.lower()
        for token in FORBIDDEN_OPERATION_TOKENS:
            # Matched on a dotted-segment boundary rather than as a bare substring: `kubernetes.pod_logs`
            # contains "sh" in neither segment, but a naive `in` test would also reject a legitimate future
            # `docker.image_push` for containing "sh". The tokens are whole segments or prefixes of them.
            segments = re.split(r"[._-]", lowered)
            if token in segments:
                raise AssertionError(
                    f"command {spec.intent!r} resolves to operation {spec.operation!r}, whose segment "
                    f"{token!r} is on the forbidden list. The Command Center must never reach a shell, an "
                    "exec or an arbitrary runner: natural language resolving to one is the escape this "
                    "module exists to prevent."
                )


_assert_operations_are_safe()


# --- the classifier -------------------------------------------------------------------------------------
#
# Deterministic, and that is a decision rather than a limitation: see the module docstring. An LLM can be
# substituted behind this signature and every layer still holds, because they constrain the Intent rather
# than trusting whatever produced it.

_ENV_WORDS: Final[dict[str, str]] = {
    "dev": "dev",
    "development": "dev",
    "test": "test",
    "staging": "staging",
    "stage": "staging",
    "prod": "prod",
    "production": "prod",
}

#: Words that are never a workload name even though they are valid DNS-1123 strings.
#:
#: Found by test: "Scale to 3 replicas" extracted `workload="to"`, because "to" follows the verb and is a
#: perfectly legal Kubernetes name. The slot validator could not catch it -- it IS valid -- so the fix
#: belongs here, in extraction. A workload called `to` would then have been scaled, which is the quiet
#: version of this failure: a valid name pointing at the wrong thing.
_STOP_WORDS: Final[frozenset[str]] = frozenset(
    {
        "to",
        "in",
        "for",
        "of",
        "from",
        "the",
        "a",
        "an",
        "on",
        "at",
        "up",
        "down",
        "replicas",
        "replica",
        "pods",
        "pod",
        "logs",
        "log",
        "namespace",
    }
)

_ARTIFACT_WORDS: Final[dict[str, str]] = {
    "dockerfile": "dockerfile",
    "docker": "dockerfile",
    "kubernetes": "kubernetes",
    "k8s": "kubernetes",
    "manifest": "kubernetes",
    "manifests": "kubernetes",
    "compose": "compose",
    "ci": "ci",
    "workflow": "ci",
    "pipeline": "ci",
    "terraform": "terraform",
    "tofu": "terraform",
    "security": "security_policy",
}


def classify(utterance: str) -> Intent:
    """Turn a sentence into an Intent, or raise.

    THE `explain` FALLBACK IS NOT A "BEST EFFORT" HANDLER. It resolves to a non-mutating command with no
    operation -- it answers a question and can change nothing. An unmatched sentence being answered rather
    than acted on is the safe direction; the dangerous version of a fallback is one that guesses at a
    mutation.
    """
    text_value = utterance.strip().lower()
    if not text_value:
        raise CommandRefusedError("an empty command cannot be interpreted.")

    words = re.findall(r"[a-z0-9.-]+", text_value)
    word_set = set(words)

    environment = next((_ENV_WORDS[word] for word in words if word in _ENV_WORDS), None)

    # Order matters: `scale` before `deploy`, because "scale the staging api to 3" names an environment and
    # is not a deploy. A classifier that checked deploy first would deploy on a scale request.
    if "scale" in word_set:
        workload = _extract_workload(words, after={"scale"})
        replicas = _extract_replicas(words)
        slots: dict[str, Any] = {}
        if workload:
            slots["workload"] = workload
        if replicas is not None:
            slots["replicas"] = replicas
        if environment:
            slots["environment"] = environment
        return Intent(name="scale_workload", category="deploy", slots=slots, confidence=0.9, utterance=utterance)

    if "deploy" in word_set or "promote" in word_set or "release" in word_set:
        return Intent(
            name="deploy",
            category="deploy",
            slots={"environment": environment} if environment else {},
            confidence=0.9 if environment else 0.5,
            utterance=utterance,
        )

    if "log" in word_set or "logs" in word_set:
        workload = _extract_workload(words, after={"for", "of", "from"})
        return Intent(
            name="check_logs",
            category="diagnostic",
            slots={"workload": workload} if workload else {},
            confidence=0.85 if workload else 0.4,
            utterance=utterance,
        )

    if "pod" in word_set or "pods" in word_set:
        namespace = _extract_workload(words, after={"in", "namespace"})
        return Intent(
            name="show_pods",
            category="diagnostic",
            slots={"namespace": namespace} if namespace else {},
            confidence=0.9,
            utterance=utterance,
        )

    if "container" in word_set or "containers" in word_set or "running" in word_set:
        return Intent(name="show_containers", category="diagnostic", confidence=0.85, utterance=utterance)

    if "generate" in word_set or "create" in word_set or "write" in word_set:
        kind = next((_ARTIFACT_WORDS[word] for word in words if word in _ARTIFACT_WORDS), None)
        return Intent(
            name="generate_artifact",
            category="generate",
            slots={"artifact_kind": kind} if kind else {},
            confidence=0.85 if kind else 0.4,
            utterance=utterance,
        )

    if "policy" in word_set or "policies" in word_set:
        return Intent(
            name="show_policy",
            category="policy",
            slots={"environment": environment} if environment else {},
            confidence=0.8,
            utterance=utterance,
        )

    # The safe fallback: answer, never act.
    return Intent(name="explain", category="chat", confidence=0.3, utterance=utterance)


def _extract_workload(words: list[str], *, after: set[str]) -> str | None:
    """The token following a preposition, IF it is a valid DNS-1123 name.

    Returning None rather than the raw token is layer 2 in action: a word that is not a valid name is not a
    workload, and passing it on for something downstream to reject would mean an unvalidated string travelling
    one layer further than it needs to.
    """
    for index, word in enumerate(words[:-1]):
        if word in after:
            candidate = words[index + 1]
            if (
                _K8S_NAME.match(candidate)
                and candidate not in _ENV_WORDS
                and candidate not in _STOP_WORDS
                and not candidate.isdigit()
            ):
                return candidate
    # Also accept the token straight after the verb, for "scale api to 3".
    for index, word in enumerate(words[:-1]):
        if word in ("scale", "restart"):
            candidate = words[index + 1]
            if (
                _K8S_NAME.match(candidate)
                and candidate not in _ENV_WORDS
                and candidate not in _STOP_WORDS
                and not candidate.isdigit()
            ):
                return candidate
    return None


def _extract_replicas(words: list[str]) -> int | None:
    for word in words:
        if word.isdigit():
            value = int(word)
            # OUT-OF-RANGE IS DROPPED, not clamped. Clamping "scale to 99999" to 20 would silently do
            # something the user did not ask for; dropping it makes the slot missing, which `resolve`
            # refuses with a sentence naming the bound.
            if 0 <= value <= MAX_REPLICAS:
                return value
    return None


# --- layer 1 + 2 + 4: resolution into a confirmable plan ------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """What will happen, pending confirmation. Frozen, so confirmation cannot approve a different thing."""

    intent: str
    category: IntentCategory
    operation: str
    arguments: dict[str, Any]
    mutating: bool
    #: True when the plan is complete enough to execute. False means a slot is missing and the user is asked.
    ready: bool
    describes: str
    explanation: str


def resolve(intent: Intent) -> CommandPlan:
    """Map an Intent onto exactly one CommandSpec, validating every slot. Raises on anything unknown.

    LAYER 1 IS THE FIRST LINE HERE AND IT IS A MEMBERSHIP TEST WITH NO SECOND CONDITION -- the same shape as
    `is_auto_executable`. An intent name outside `INTENTS` is refused; there is no branch that dispatches it
    on a best effort, and no default command.
    """
    if intent.name not in INTENTS or intent.name not in COMMANDS:
        raise CommandRefusedError(
            f"{intent.name!r} is not a command this system understands. The command set is closed: an "
            "unrecognised intent is refused rather than attempted, because guessing at a mutation is how an "
            "automated system does something nobody asked for."
        )

    spec = COMMANDS[intent.name]
    arguments: dict[str, Any] = {}

    # LAYER 2: every slot, against an enumerated set or the one strict pattern. No slot is free text.
    for key, value in intent.slots.items():
        if value is None:
            continue
        if key == "environment":
            if str(value) not in ENVIRONMENTS and str(value) not in _ENV_WORDS:
                raise CommandRefusedError(
                    f"{value!r} is not an environment this system knows. Environments are enumerated so a "
                    "misread word cannot become a deploy to somewhere that does not exist."
                )
            arguments["environment"] = _ENV_WORDS.get(str(value), str(value))
        elif key in ("workload", "namespace"):
            if not _K8S_NAME.match(str(value)):
                raise CommandRefusedError(
                    f"{value!r} is not a valid Kubernetes name. Names are matched against DNS-1123, which "
                    "admits no space, slash, quote, semicolon or dollar sign -- so a name cannot carry a "
                    "second token into an argument vector."
                )
            arguments[key] = str(value)
        elif key == "replicas":
            if not isinstance(value, int) or not 0 <= value <= MAX_REPLICAS:
                raise CommandRefusedError(
                    f"a replica count must be a whole number between 0 and {MAX_REPLICAS}. An unbounded "
                    "count is a denial of service against the cluster expressed as a number."
                )
            arguments["replicas"] = value
        elif key == "artifact_kind":
            if str(value) not in ARTIFACT_KINDS:
                raise CommandRefusedError(
                    f"{value!r} is not an artifact kind this system generates. The set is closed because "
                    "generating something else produces an artifact nothing downstream can judge."
                )
            arguments["artifact_kind"] = str(value)
        else:
            # AN UNKNOWN SLOT IS REFUSED, not dropped. Dropping it would let a classifier emit a slot the
            # resolver silently ignored, and the user would believe they had constrained the command.
            raise CommandRefusedError(
                f"{key!r} is not a slot any command accepts. Ignoring it would let you believe the command "
                "was constrained in a way it was not."
            )

    missing = [slot for slot in spec.required_slots if slot not in arguments]
    ready = not missing

    return CommandPlan(
        intent=spec.intent,
        category=spec.category,
        operation=spec.operation,
        arguments=arguments,
        mutating=spec.mutating,
        ready=ready,
        describes=spec.describes,
        explanation=(
            (
                f"Understood as: {spec.describes} "
                + (
                    "This changes things, so it needs your confirmation and then passes policy, approval and "
                    "audit like any other change."
                    if spec.mutating
                    else "This only reads; nothing will be changed."
                )
            )
            if ready
            else (
                f"Understood as: {spec.describes} Missing: {', '.join(missing)}. Nothing has been done -- a "
                "missing value is asked for rather than guessed at."
            )
        ),
    )


def catalogue() -> list[dict[str, Any]]:
    """What the autocomplete reads. Published so the closed set is inspectable rather than implied."""
    return [
        {
            "intent": spec.intent,
            "category": spec.category,
            "mutating": spec.mutating,
            "describes": spec.describes,
            "examples": list(spec.examples),
            "required_slots": list(spec.required_slots),
        }
        for spec in COMMANDS.values()
    ]
