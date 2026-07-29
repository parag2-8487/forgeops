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
    ) -> None:
        self._warn = warn_threshold
        self._block = block_threshold

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
        if stateful_deletions or score >= self._block:
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
