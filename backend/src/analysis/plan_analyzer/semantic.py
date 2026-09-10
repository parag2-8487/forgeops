# SPDX-License-Identifier: FSL-1.1-ALv2
"""Semantic Plan Analyzer: destructive-action detection and blast-radius computation.

Deterministic and monotone: adding a destructive action can never lower the
score or soften the verdict (P-11). No LLM is involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .models import Finding, PlanDocument, Severity, StageContext


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    DELETE = "delete"
    NOOP = "no-op"


DESTRUCTIVE: frozenset[Action] = frozenset({Action.DELETE, Action.REPLACE})

# Weights are configuration, not magic numbers.
ACTION_WEIGHT: Mapping[Action, int] = {
    Action.NOOP: 0,
    Action.CREATE: 1,
    Action.UPDATE: 2,
    Action.REPLACE: 5,
    Action.DELETE: 8,
}

# Resource classes whose loss is unrecoverable get a multiplier.
CLASS_MULTIPLIER: Mapping[str, int] = {
    "stateful": 3,
    "network": 2,
    "iam": 3,
    "compute": 1,
    "unknown": 2,
}

# Heuristic: resource types that are stateful (databases, volumes, etc.)
STATEFUL_TYPES: frozenset[str] = frozenset(
    {
        "aws_db_instance",
        "aws_rds_cluster",
        "aws_dynamodb_table",
        "aws_s3_bucket",
        "aws_ebs_volume",
        "aws_efs_file_system",
        "azurerm_sql_database",
        "azurerm_storage_account",
        "google_sql_database_instance",
        "google_storage_bucket",
        "postgresql_database",
        "mysql_database",
        "null_resource",  # NOT stateful, but useful for testing
    }
)

NETWORK_TYPES: frozenset[str] = frozenset(
    {
        "aws_vpc",
        "aws_subnet",
        "aws_security_group",
        "aws_route_table",
        "azurerm_virtual_network",
        "google_compute_network",
    }
)

IAM_TYPES: frozenset[str] = frozenset(
    {
        "aws_iam_role",
        "aws_iam_policy",
        "aws_iam_user",
        "azurerm_role_assignment",
        "google_project_iam_member",
    }
)


def classify_resource(resource_type: str) -> str:
    """Classify a resource type into a category for blast-radius scoring."""
    if resource_type in STATEFUL_TYPES:
        return "stateful"
    if resource_type in NETWORK_TYPES:
        return "network"
    if resource_type in IAM_TYPES:
        return "iam"
    # Default to 'compute' for known compute resources, 'unknown' otherwise
    if resource_type.startswith(
        ("aws_instance", "aws_ecs", "aws_lambda", "azurerm_virtual_machine", "google_compute_instance")
    ):
        return "compute"
    return "unknown"


def normalize_action(actions: list[str]) -> Action:
    """Normalize the OpenTofu actions list to a single Action."""
    if not actions:
        return Action.NOOP
    if actions == ["no-op"]:
        return Action.NOOP
    if actions == ["create"]:
        return Action.CREATE
    if actions == ["update"]:
        return Action.UPDATE
    if actions == ["delete"]:
        return Action.DELETE
    if "delete" in actions and "create" in actions:
        return Action.REPLACE  # delete+create = replace
    if actions == ["create", "delete"]:
        return Action.REPLACE
    if actions == ["delete", "create"]:
        return Action.REPLACE
    # Default: treat unknown actions conservatively as update
    return Action.UPDATE


@dataclass(frozen=True)
class BlastRadius:
    score: int
    destructive_count: int
    affected_resources: int
    stateful_deletions: tuple[str, ...]
    verdict: Literal["allow", "warn", "block"]


class SemanticPlanAnalyzer:
    """Deterministic blast-radius computation.

    Monotone: adding a destructive action can never lower the score or soften
    the verdict (P-11).
    """

    def __init__(
        self,
        *,
        warn_threshold: int = 10,
        block_threshold: int = 25,
        size_alone_blocks: bool = True,
    ) -> None:
        """Calibration, not behaviour: the scoring is identical for every caller.

        `size_alone_blocks` exists because `score` is a scalar that has already lost the action
        mix, and the two units this analyser is asked about disagree about what a large score
        MEANS. For a Terraform plan, size and danger travel together — the score only climbs
        because resources are being replaced or destroyed — so size alone blocking is correct and
        this stays `True`.

        For a file change set (`plan_from_change_items`) it is not. Every item classifies as
        `unknown` and a *created* file contributes 2 points, so thirteen brand-new files reach the
        default block threshold of 25 while destroying nothing. Blocking is terminal and has no
        recourse, whereas "many files at once" is precisely what the approval gate is for. Passing
        `False` keeps destruction blocking and lets size route to approval instead of a dead end.

        Deliberately a calibration flag rather than a second analyser: D-65 rejected a separate
        implementation for file change sets because two blast-radius implementations are how two
        answers to the same question come to disagree. This changes which conclusion is drawn from
        the evidence, not how the evidence is computed.
        """
        self._warn = warn_threshold
        self._block = block_threshold
        self._size_alone_blocks = size_alone_blocks

    def analyse(self, doc: PlanDocument) -> BlastRadius:
        score = 0
        destructive_count = 0
        affected_resources = 0
        stateful_deletions: list[str] = []

        for rc in doc.resource_changes:
            change = rc.get("change", {})
            actions = change.get("actions", [])
            action = normalize_action(actions)

            if action == Action.NOOP:
                continue

            affected_resources += 1
            resource_type = rc.get("type", "")
            address = rc.get("address", "unknown")
            resource_class = classify_resource(resource_type)

            weight = ACTION_WEIGHT.get(action, 2)
            multiplier = CLASS_MULTIPLIER.get(resource_class, 2)
            score += weight * multiplier

            if action in DESTRUCTIVE:
                destructive_count += 1
                if resource_class == "stateful":
                    stateful_deletions.append(address)

        # Verdict is a pure, monotone function of the accumulated evidence.
        #
        # Monotonicity (P-11) survives `size_alone_blocks=False`: adding a destructive action still
        # only ever raises `score` and `destructive_count`, and both appear on the blocking side of
        # this rule. What the flag removes is a block reachable with `destructive_count == 0`, which
        # no destructive action was needed to trigger in the first place.
        size_blocks = score >= self._block and (self._size_alone_blocks or destructive_count > 0)
        if stateful_deletions or size_blocks:
            verdict = "block"
        elif destructive_count > 0 or score >= self._warn:
            verdict = "warn"
        else:
            verdict = "allow"

        return BlastRadius(
            score=score,
            destructive_count=destructive_count,
            affected_resources=affected_resources,
            stateful_deletions=tuple(stateful_deletions),
            verdict=verdict,
        )


class SemanticStage:
    """Pipeline stage wrapper for the SemanticPlanAnalyzer."""

    name = "semantic"

    def __init__(self, analyzer: SemanticPlanAnalyzer | None = None) -> None:
        self._analyzer = analyzer or SemanticPlanAnalyzer()

    async def run(self, doc: PlanDocument, ctx: StageContext) -> list[Finding]:
        result = self._analyzer.analyse(doc)
        findings: list[Finding] = []

        if result.destructive_count > 0:
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.WARNING,
                    code="DESTRUCTIVE_ACTIONS",
                    message=f"{result.destructive_count} destructive action(s) detected",
                )
            )

        for addr in result.stateful_deletions:
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.ERROR,
                    code="STATEFUL_DELETION",
                    message=f"Stateful resource {addr} will be deleted",
                    resource=addr,
                )
            )

        if result.verdict == "block":
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.ERROR,
                    code="BLAST_RADIUS_BLOCK",
                    message=f"Blast radius score {result.score} exceeds block threshold",
                )
            )

        return findings
