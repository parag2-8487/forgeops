# SPDX-License-Identifier: FSL-1.1-ALv2
"""Tests for the Semantic Plan Analyzer (task 14.2)."""

from src.analysis.plan_analyzer import PlanDocument
from src.analysis.plan_analyzer.semantic import (
    Action,
    SemanticPlanAnalyzer,
    classify_resource,
    normalize_action,
)
from src.governance.chokepoint import (
    FILE_BLOCK_SCORE,
    ChangeItemRequest,
    file_change_set_analyzer,
    plan_from_change_items,
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


class TestTheFileChangeSetCalibration:
    """`size_alone_blocks=False`, and the run that proved it was needed.

    Every case here is expressed through `plan_from_change_items` and the real factory rather than
    a hand-built plan, because the defect was not in the scoring — it was in which calibration the
    chokepoint handed a file change set.
    """

    def test_the_generation_that_was_refused_is_no_longer_blocked(self):
        """Run ae72a6ad's exact ten items: 7 created + 3 updated, score 26, deleting nothing.

        Under the Terraform defaults this scored 26 against a block threshold of 25 and was refused
        outright with no approval path. It is a complete, correct deployable unit.
        """
        items = [
            ChangeItemRequest(file_path=path, action="create", new_content="x\n")
            for path in (
                "charts/test-2/Chart.yaml",
                "charts/test-2/templates/deployment.yaml",
                "charts/test-2/templates/_helpers.tpl",
                "charts/test-2/values.yaml",
                ".github/workflows/build.yml",
                "infra/main.tf",
                "k8s/ingress.yaml",
            )
        ] + [
            ChangeItemRequest(file_path=path, action="update", old_content="x\n", new_content="y\n")
            for path in ("Dockerfile", "k8s/deployment.yaml", "k8s/service.yaml")
        ]

        result = file_change_set_analyzer().analyse(plan_from_change_items(items))

        assert result.score == 7 * 2 + 3 * 4 == 26, "the arithmetic the defect turned on"
        assert result.destructive_count == 0, "nothing here destroys anything"
        assert result.verdict != "block", "a correct generation must not hit a terminal refusal"
        assert result.verdict == "warn", "it still wants a human, which is what approval is for"

    def test_the_default_calibration_still_blocks_that_same_change_set(self):
        """The contrast, so this test fails if the two calibrations are ever collapsed into one."""
        items = [ChangeItemRequest(file_path=f"new-{i}.yml", action="create", new_content="x\n") for i in range(13)]
        plan = plan_from_change_items(items)

        assert SemanticPlanAnalyzer().analyse(plan).verdict == "block"
        assert file_change_set_analyzer().analyse(plan).verdict == "warn"

    def test_bulk_deletion_is_still_refused_outright(self):
        """The block the suite always asserted. Four deletions: 4 x 8 x 2 = 64."""
        items = [ChangeItemRequest(file_path=f"deleted-{i}.yml", action="delete", old_content="x\n") for i in range(4)]

        result = file_change_set_analyzer().analyse(plan_from_change_items(items))

        assert result.score == 64
        assert result.destructive_count == 4
        assert result.verdict == "block", "destruction at scale must still have no approval path"

    def test_fewer_deletions_reach_approval_rather_than_a_dead_end(self):
        """Three deletions score 48 — under the file threshold, so a human decides."""
        items = [ChangeItemRequest(file_path=f"deleted-{i}.yml", action="delete", old_content="x\n") for i in range(3)]

        result = file_change_set_analyzer().analyse(plan_from_change_items(items))

        assert result.score == 48
        assert result.verdict == "warn"

    def test_a_single_deletion_never_passes_silently(self):
        """`destructive_count > 0` forces at least `warn` whatever the thresholds say."""
        items = [ChangeItemRequest(file_path="gone.yml", action="delete", old_content="x\n")]

        result = file_change_set_analyzer().analyse(plan_from_change_items(items))

        assert result.verdict == "warn", "one deletion is never `allow`"

    def test_a_large_creation_set_cannot_block_however_big_it_gets(self):
        """The property `size_alone_blocks=False` actually asserts.

        Forty new files score 80, well past `FILE_BLOCK_SCORE`. It stays out of `block` because
        `destructive_count` is zero, so growing a non-destructive change set can never turn it into
        a refusal with no recourse.
        """
        items = [ChangeItemRequest(file_path=f"new-{i}.yml", action="create", new_content="x\n") for i in range(40)]

        result = file_change_set_analyzer().analyse(plan_from_change_items(items))

        assert result.score == 80 > FILE_BLOCK_SCORE
        assert result.verdict == "warn"

    def test_destruction_inside_a_large_change_set_still_blocks(self):
        """Size and destruction together are the blocking combination, and remain so."""
        items = [ChangeItemRequest(file_path=f"new-{i}.yml", action="create", new_content="x\n") for i in range(30)] + [
            ChangeItemRequest(file_path="gone.yml", action="delete", old_content="x\n")
        ]

        result = file_change_set_analyzer().analyse(plan_from_change_items(items))

        assert result.score == 60 + 16 == 76
        assert result.destructive_count == 1
        assert result.verdict == "block", "one deletion in a huge set is exactly what should stop"

    def test_monotonicity_holds_under_the_file_calibration(self):
        """P-11: adding a destructive action can never soften the verdict."""
        analyzer = file_change_set_analyzer()
        order = {"allow": 0, "warn": 1, "block": 2}
        items: list[ChangeItemRequest] = []
        previous = 0

        for index in range(6):
            items.append(ChangeItemRequest(file_path=f"deleted-{index}.yml", action="delete", old_content="x\n"))
            current = order[analyzer.analyse(plan_from_change_items(items)).verdict]
            assert current >= previous, "the verdict softened as destruction was added"
            previous = current

        assert previous == order["block"]
