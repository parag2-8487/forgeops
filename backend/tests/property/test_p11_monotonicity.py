# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-11: SemanticPlanAnalyzer is deterministic and monotone."""

from hypothesis import given, settings
from hypothesis import strategies as st
from src.analysis.plan_analyzer import PlanDocument
from src.analysis.plan_analyzer.semantic import (
    SemanticPlanAnalyzer,
)

VERDICT_ORDER = {"allow": 0, "warn": 1, "block": 2}


def resource_change_strategy():
    action = st.sampled_from(["create", "update", "delete", "no-op", ["delete", "create"]])
    rtype = st.sampled_from(
        [
            "aws_instance",
            "aws_db_instance",
            "aws_vpc",
            "aws_iam_role",
            "null_resource",
            "random_thing",
            "aws_s3_bucket",
        ]
    )
    return st.fixed_dictionaries(
        {
            "address": st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnop._"),
            "type": rtype,
            "change": st.fixed_dictionaries(
                {
                    "actions": action.map(lambda a: a if isinstance(a, list) else [a]),
                }
            ),
        }
    )


@given(plan=st.lists(resource_change_strategy(), min_size=0, max_size=10))
@settings(max_examples=200)
def test_deterministic(plan):
    """Same input always produces same output."""
    analyzer = SemanticPlanAnalyzer()
    doc = PlanDocument(raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=plan)
    r1 = analyzer.analyse(doc)
    r2 = analyzer.analyse(doc)
    assert r1 == r2


@given(
    base_plan=st.lists(resource_change_strategy(), min_size=0, max_size=5),
    extra=resource_change_strategy(),
)
@settings(max_examples=200)
def test_score_monotone(base_plan, extra):
    """Appending any action never decreases the score."""
    analyzer = SemanticPlanAnalyzer()
    base_doc = PlanDocument(raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=base_plan)
    ext_doc = PlanDocument(
        raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=base_plan + [extra]
    )
    base_r = analyzer.analyse(base_doc)
    ext_r = analyzer.analyse(ext_doc)
    assert ext_r.score >= base_r.score


@given(
    base_plan=st.lists(resource_change_strategy(), min_size=0, max_size=5),
    extra=resource_change_strategy(),
)
@settings(max_examples=200)
def test_verdict_monotone(base_plan, extra):
    """Appending any action never softens the verdict."""
    analyzer = SemanticPlanAnalyzer()
    base_doc = PlanDocument(raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=base_plan)
    ext_doc = PlanDocument(
        raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=base_plan + [extra]
    )
    base_r = analyzer.analyse(base_doc)
    ext_r = analyzer.analyse(ext_doc)
    assert VERDICT_ORDER[ext_r.verdict] >= VERDICT_ORDER[base_r.verdict]


@given(plan=st.lists(resource_change_strategy(), min_size=0, max_size=10))
@settings(max_examples=200)
def test_destructive_count_bounded(plan):
    """destructive_count <= affected_resources always."""
    analyzer = SemanticPlanAnalyzer()
    doc = PlanDocument(raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=plan)
    r = analyzer.analyse(doc)
    assert r.destructive_count <= r.affected_resources


@given(plan=st.lists(resource_change_strategy(), min_size=1, max_size=10))
@settings(max_examples=200)
def test_stateful_deletion_forces_block(plan):
    """Any stateful deletion forces verdict=block."""
    analyzer = SemanticPlanAnalyzer()
    doc = PlanDocument(raw={}, format_version="1.2", terraform_version="1.12.5", resource_changes=plan)
    r = analyzer.analyse(doc)
    if r.stateful_deletions:
        assert r.verdict == "block"
