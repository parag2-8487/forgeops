# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the Semantic Plan Analyzer (task 14.2)."""

from src.analysis.plan_analyzer import PlanDocument
from src.analysis.plan_analyzer.semantic import (
    Action,
    SemanticPlanAnalyzer,
    classify_resource,
    normalize_action,
)


def make_plan(resource_changes: list) -> PlanDocument:
    return PlanDocument(
        raw={"resource_changes": resource_changes},
        format_version="1.2",
        terraform_version="1.12.5",
        resource_changes=resource_changes,
    )


class TestNormalizeAction:
    def test_no_op(self):
        assert normalize_action(["no-op"]) == Action.NOOP

    def test_create(self):
        assert normalize_action(["create"]) == Action.CREATE

    def test_update(self):
        assert normalize_action(["update"]) == Action.UPDATE

    def test_delete(self):
        assert normalize_action(["delete"]) == Action.DELETE

    def test_replace_delete_create(self):
        assert normalize_action(["delete", "create"]) == Action.REPLACE

    def test_replace_create_delete(self):
        assert normalize_action(["create", "delete"]) == Action.REPLACE

    def test_empty(self):
        assert normalize_action([]) == Action.NOOP


class TestClassifyResource:
    def test_stateful(self):
        assert classify_resource("aws_db_instance") == "stateful"
        assert classify_resource("aws_s3_bucket") == "stateful"

    def test_network(self):
        assert classify_resource("aws_vpc") == "network"

    def test_iam(self):
        assert classify_resource("aws_iam_role") == "iam"

    def test_unknown(self):
        assert classify_resource("random_thing") == "unknown"


class TestSemanticPlanAnalyzer:
    def test_empty_plan(self):
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan([])
        result = analyzer.analyse(doc)
        assert result.score == 0
        assert result.affected_resources == 0
        assert result.destructive_count == 0
        assert result.verdict == "allow"

    def test_create_only(self):
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan(
            [
                {
                    "address": "null_resource.test",
                    "type": "null_resource",
                    "change": {"actions": ["create"]},
                }
            ]
        )
        result = analyzer.analyse(doc)
        assert result.affected_resources == 1
        assert result.destructive_count == 0
        assert result.verdict == "allow"
        # null_resource is stateful, weight=1, multiplier=3
        assert result.score == 1 * 3

    def test_delete_triggers_warn(self):
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan(
            [
                {
                    "address": "aws_instance.web",
                    "type": "aws_instance",
                    "change": {"actions": ["delete"]},
                }
            ]
        )
        result = analyzer.analyse(doc)
        assert result.destructive_count == 1
        assert result.verdict == "warn"

    def test_stateful_delete_forces_block(self):
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan(
            [
                {
                    "address": "aws_db_instance.prod",
                    "type": "aws_db_instance",
                    "change": {"actions": ["delete"]},
                }
            ]
        )
        result = analyzer.analyse(doc)
        assert result.stateful_deletions == ("aws_db_instance.prod",)
        assert result.verdict == "block"

    def test_score_monotone(self):
        """Adding a destructive action never lowers the score."""
        analyzer = SemanticPlanAnalyzer()
        base = make_plan(
            [
                {
                    "address": "aws_instance.a",
                    "type": "aws_instance",
                    "change": {"actions": ["create"]},
                }
            ]
        )
        extended = make_plan(
            [
                {"address": "aws_instance.a", "type": "aws_instance", "change": {"actions": ["create"]}},
                {"address": "aws_instance.b", "type": "aws_instance", "change": {"actions": ["delete"]}},
            ]
        )
        base_result = analyzer.analyse(base)
        ext_result = analyzer.analyse(extended)
        assert ext_result.score >= base_result.score

    def test_verdict_monotone(self):
        """Adding a destructive action never softens the verdict."""
        verdict_order = {"allow": 0, "warn": 1, "block": 2}
        analyzer = SemanticPlanAnalyzer()

        base = make_plan(
            [
                {
                    "address": "aws_instance.a",
                    "type": "aws_instance",
                    "change": {"actions": ["update"]},
                }
            ]
        )
        extended = make_plan(
            [
                {"address": "aws_instance.a", "type": "aws_instance", "change": {"actions": ["update"]}},
                {"address": "aws_db_instance.x", "type": "aws_db_instance", "change": {"actions": ["delete"]}},
            ]
        )
        base_result = analyzer.analyse(base)
        ext_result = analyzer.analyse(extended)
        assert verdict_order[ext_result.verdict] >= verdict_order[base_result.verdict]

    def test_deterministic(self):
        """Same input always produces same output."""
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan(
            [
                {
                    "address": "aws_vpc.main",
                    "type": "aws_vpc",
                    "change": {"actions": ["delete", "create"]},
                }
            ]
        )
        r1 = analyzer.analyse(doc)
        r2 = analyzer.analyse(doc)
        assert r1 == r2

    def test_noop_excluded(self):
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan(
            [
                {
                    "address": "aws_instance.a",
                    "type": "aws_instance",
                    "change": {"actions": ["no-op"]},
                }
            ]
        )
        result = analyzer.analyse(doc)
        assert result.affected_resources == 0
        assert result.score == 0

    def test_unknown_class_conservative(self):
        """Unknown resource types get the 'unknown' multiplier (conservative)."""
        analyzer = SemanticPlanAnalyzer()
        doc = make_plan(
            [
                {
                    "address": "some_provider_thing.x",
                    "type": "some_provider_thing",
                    "change": {"actions": ["delete"]},
                }
            ]
        )
        result = analyzer.analyse(doc)
        # weight=8 (delete), multiplier=2 (unknown)
        assert result.score == 8 * 2

    def test_threshold_customization(self):
        analyzer = SemanticPlanAnalyzer(warn_threshold=5, block_threshold=10)
        doc = make_plan(
            [
                {
                    "address": "some_provider_thing.a",
                    "type": "some_provider_thing",
                    "change": {"actions": ["delete"]},  # score=8*2=16 (unknown class)
                }
            ]
        )
        result = analyzer.analyse(doc)
        assert result.verdict == "block"  # 16 >= 10
