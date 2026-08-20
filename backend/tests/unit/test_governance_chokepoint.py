# SPDX-License-Identifier: FSL-1.1-ALv2
"""The chokepoint's contracts, without a database (design.md §2.2, §11.6, Appendix A.3; leaf 7.5).

What belongs here and what does not
-----------------------------------
The six stages are a property of a **transaction**, so they are asserted against a real
PostgreSQL in `tests/integration/test_governance_chokepoint.py`. What can be asserted here is
everything the transaction is not: the input types' refusals, the `PlanFrom` translation's
determinism, the fail-closed defaults, and — the important one — the *structural* claim that
`mint_authority` and `sign_envelope` each have exactly one call site in the whole backend and it
is inside `governance/chokepoint.py`.

That last group is deliberately an `ast` walk rather than prose. §2.2's claim is that the stages
"cannot be skipped"; a second mint call site anywhere would falsify it, and nothing else in the
suite would notice. Leaf 7.7 generalises the walk into Q-03 over generated call graphs; these
are the fixed assertions the generated ones will subsume, kept because a property test that
quantifies over generated inputs still benefits from one example nobody can argue about.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from src.analysis.plan_analyzer.semantic import SemanticPlanAnalyzer
from src.governance.chokepoint import (
    APPLY_OPERATION,
    FILE_RESOURCE_TYPE,
    REVERT_OPERATION,
    ROLLBACK_HANDLE_TTL,
    ChangeItemRequest,
    CommandSink,
    GovernanceAction,
    MutationRequest,
    SignedCommand,
    UnavailableCommandSink,
    plan_from_change_items,
)
from src.governance.policy import (
    GovernanceDecision,
    GovernancePolicySource,
    PolicyDocumentUndefinedError,
    PolicySourceError,
    PolicySourceUnavailableError,
    UnavailableGovernancePolicy,
)
from src.governance.sequencing import NONCE_HEX_LENGTH, EnvelopeSequencer, generate_nonce

pytestmark = pytest.mark.mandatory

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
CHOKEPOINT = SRC_ROOT / "governance" / "chokepoint.py"


def _call_sites(function_name: str) -> list[str]:
    """Every `path:line` in `backend/src/**` that calls `function_name` by that name.

    Parses rather than imports, for the reason `governance/primitives.py` gives: importing
    `src/**` to enumerate anything runs module-level code inside a static check.
    """
    found: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name == function_name:
                found.append(f"{path.relative_to(SRC_ROOT).as_posix()}:{node.lineno}")
    return found


class TestTheMintHasExactlyOneCallSite:
    """§2.2's claim, as an assertion rather than a diagram."""

    def test_mint_authority_is_called_from_one_place(self) -> None:
        sites = _call_sites("mint_authority")
        assert len(sites) == 1, (
            f"mint_authority is called from {sites}; §2.2 requires exactly one mint path, and a "
            "second call site is a second way to produce authority"
        )
        assert sites[0].startswith("governance/chokepoint.py"), sites

    def test_sign_envelope_is_called_from_one_place(self) -> None:
        sites = _call_sites("sign_envelope")
        assert len(sites) == 1, f"sign_envelope is called from {sites}; §2.2.2 confines it to the chokepoint"
        assert sites[0].startswith("governance/chokepoint.py"), sites

    def test_the_signing_key_scope_is_entered_from_one_place(self) -> None:
        """D-60 bans the setter so a missing scope always raises. One entry point keeps that true."""
        sites = _call_sites("signing_key_scope")
        assert len(sites) == 1, f"signing_key_scope is entered from {sites}"
        assert sites[0].startswith("governance/chokepoint.py"), sites

    def test_the_device_envelope_key_is_fetched_from_one_place(self) -> None:
        """§11.2: "a service that can fetch a signing key is a service that can forge a command"."""
        sites = [site for site in _call_sites("envelope_key") if not site.startswith("auth/devices.py")]
        assert len(sites) == 1, f"envelope_key is fetched from {sites} outside auth/devices.py"
        assert sites[0].startswith("governance/chokepoint.py"), sites

    def test_the_mint_is_reached_through_one_private_method(self) -> None:
        """`submit`, `approve` and `revert` must all mint through `_mint_and_sign`.

        Three public transits reaching the mint three different ways would mean "what must be
        true before an authority exists" needs three readings.
        """
        tree = ast.parse(CHOKEPOINT.read_text(encoding="utf-8"), filename=str(CHOKEPOINT))
        callers = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "mint_authority"
        }
        assert callers == {"_mint_and_sign"} or callers == set(), callers
        direct = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "mint_authority"
        }
        assert direct == {"_mint_and_sign"}, f"mint_authority is called from {direct}, not only _mint_and_sign"


class TestTheInputTypeRefusesWhatCannotBeCompiled:
    def test_an_empty_change_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one change item"):
            MutationRequest(project_id=uuid.uuid4(), items=(), reason="why")

    def test_a_missing_reason_is_refused_because_the_audit_row_requires_one(self) -> None:
        with pytest.raises(ValueError, match="state a reason"):
            MutationRequest(
                project_id=uuid.uuid4(),
                items=(ChangeItemRequest(file_path="a.txt", action="create", new_content="x"),),
                reason="   ",
            )

    def test_an_unknown_origin_is_refused(self) -> None:
        with pytest.raises(ValueError, match="origin must be one of"):
            MutationRequest(
                project_id=uuid.uuid4(),
                items=(ChangeItemRequest(file_path="a.txt", action="create", new_content="x"),),
                reason="why",
                origin="ad-hoc",
            )

    def test_the_same_file_twice_is_refused(self) -> None:
        """Two items for one path make the apply ORDER decide the result, which no caller states."""
        item = ChangeItemRequest(file_path="a.txt", action="create", new_content="x")
        with pytest.raises(ValueError, match="same file twice"):
            MutationRequest(project_id=uuid.uuid4(), items=(item, item), reason="why")


class TestAChangeItemKnowsWhatItOverwrites:
    def test_an_update_without_a_pre_image_is_refused(self) -> None:
        """`change_items.old_hash` is what lets the agent refuse a stale apply (§6.3)."""
        with pytest.raises(ValueError, match="must carry old_content"):
            ChangeItemRequest(file_path="a.txt", action="update", new_content="new")

    def test_a_delete_without_a_pre_image_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must carry old_content"):
            ChangeItemRequest(file_path="a.txt", action="delete")

    def test_a_create_without_content_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must carry new_content"):
            ChangeItemRequest(file_path="a.txt", action="create")

    def test_a_delete_carrying_new_content_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not carry new_content"):
            ChangeItemRequest(file_path="a.txt", action="delete", old_content="old", new_content="x")

    def test_an_unknown_action_is_refused(self) -> None:
        with pytest.raises(ValueError, match="action must be one of"):
            ChangeItemRequest(file_path="a.txt", action="rename", old_content="a", new_content="b")

    def test_a_blank_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must name a file path"):
            ChangeItemRequest(file_path="  ", action="create", new_content="x")


class TestThePlanTranslation:
    """D-65: change items render as one synthetic resource type, so the score is a function of
    the action mix and the cardinality rather than of a cloud resource class they do not have."""

    def _items(self, *actions: str) -> tuple[ChangeItemRequest, ...]:
        made = []
        for index, action in enumerate(actions):
            if action == "create":
                made.append(ChangeItemRequest(file_path=f"f{index}.txt", action="create", new_content="new"))
            elif action == "delete":
                made.append(ChangeItemRequest(file_path=f"f{index}.txt", action="delete", old_content="old"))
            else:
                made.append(
                    ChangeItemRequest(file_path=f"f{index}.txt", action="update", old_content="old", new_content="new")
                )
        return tuple(made)

    def test_every_item_becomes_one_resource_change(self) -> None:
        plan = plan_from_change_items(self._items("create", "update", "delete"))
        assert len(plan.resource_changes) == 3
        assert {change["type"] for change in plan.resource_changes} == {FILE_RESOURCE_TYPE}

    def test_the_verdict_is_independent_of_item_order(self) -> None:
        """`SemanticPlanAnalyzer` sums per-item contributions, so a permutation must agree."""
        analyzer = SemanticPlanAnalyzer()
        items = self._items("create", "delete", "update", "create")
        forward = analyzer.analyse(plan_from_change_items(items))
        backward = analyzer.analyse(plan_from_change_items(tuple(reversed(items))))
        assert forward == backward

    def test_a_deletion_makes_the_verdict_at_least_warn(self) -> None:
        """Monotone in the direction that matters (P-11): destructive work needs a human."""
        analyzer = SemanticPlanAnalyzer()
        assert analyzer.analyse(plan_from_change_items(self._items("create"))).verdict == "allow"
        assert analyzer.analyse(plan_from_change_items(self._items("delete"))).verdict in ("warn", "block")

    def test_adding_a_deletion_never_lowers_the_score(self) -> None:
        analyzer = SemanticPlanAnalyzer()
        smaller = analyzer.analyse(plan_from_change_items(self._items("create", "update")))
        larger = analyzer.analyse(plan_from_change_items(self._items("create", "update", "delete")))
        assert larger.score >= smaller.score
        assert larger.destructive_count >= smaller.destructive_count

    def test_a_large_destructive_set_blocks(self) -> None:
        analyzer = SemanticPlanAnalyzer()
        assert analyzer.analyse(plan_from_change_items(self._items(*(["delete"] * 4)))).verdict == "block"

    def test_a_file_change_set_never_claims_a_stateful_deletion(self) -> None:
        """D-65's stated cost, asserted so nobody later reads the absence as a bug."""
        analyzer = SemanticPlanAnalyzer()
        report = analyzer.analyse(plan_from_change_items(self._items(*(["delete"] * 6))))
        assert report.stateful_deletions == ()


class TestTheFailClosedDefaults:
    async def test_the_default_policy_source_reports_an_outage(self) -> None:
        with pytest.raises(PolicySourceUnavailableError, match="no governance policy engine"):
            await UnavailableGovernancePolicy().evaluate(payload={})

    async def test_the_default_command_sink_refuses_delivery(self) -> None:
        """A sink that silently dropped commands would report success while nothing ever ran."""
        from src.core.errors import ProblemException

        with pytest.raises(ProblemException) as raised:
            await UnavailableCommandSink().send_command(
                device_id=uuid.uuid4(),
                command=SignedCommand(envelope={}, signature="sig", digest="d", device_id=uuid.uuid4()),
            )
        assert raised.value.problem.status == 409
        assert raised.value.problem.type.endswith("/device-not-connected")

    def test_the_two_policy_failures_are_siblings_not_a_hierarchy(self) -> None:
        """If one subclassed the other, the chokepoint's two `except` clauses would collapse and
        one of the translations would silently stop happening — during an outage."""
        assert issubclass(PolicyDocumentUndefinedError, PolicySourceError)
        assert issubclass(PolicySourceUnavailableError, PolicySourceError)
        assert not issubclass(PolicyDocumentUndefinedError, PolicySourceUnavailableError)
        assert not issubclass(PolicySourceUnavailableError, PolicyDocumentUndefinedError)

    def test_the_default_sink_and_policy_satisfy_their_protocols(self) -> None:
        """`runtime_checkable` only checks method presence, so this is a shape check, not a
        signature check. The signature check is §0.4.2's conformance suite, which binds every
        call site in this module against the real classes."""
        assert isinstance(UnavailableCommandSink(), CommandSink)
        assert isinstance(UnavailableGovernancePolicy(), GovernancePolicySource)


class TestTheDecisionType:
    def test_a_reasonless_decision_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty reason"):
            GovernanceDecision(result="allow", reason="  ")

    def test_an_unknown_result_is_refused(self) -> None:
        with pytest.raises(ValueError, match="policy result must be one of"):
            GovernanceDecision(result="maybe", reason="why")  # type: ignore[arg-type]

    def test_require_approval_is_a_first_class_result(self) -> None:
        """Not "allow plus a flag": stage 2 consumes it, and a boolean beside an allow is one
        refactor away from being dropped."""
        assert GovernanceDecision(result="require_approval", reason="prod").result == "require_approval"


class TestTheConstants:
    def test_the_two_mutating_operations_are_the_ones_in_the_catalogue(self) -> None:
        assert (APPLY_OPERATION, REVERT_OPERATION) == ("changeset.apply", "changeset.revert")

    def test_the_rollback_handle_ttl_is_data_not_configuration(self) -> None:
        """A deployment that could set this to zero would pass every test while shipping a
        product with no rollback (criterion 6)."""
        from src.core.config import Settings

        assert ROLLBACK_HANDLE_TTL.days == 30
        assert not any("rollback" in name for name in Settings.model_fields)

    def test_the_audit_action_vocabulary_is_closed_and_covers_every_transit(self) -> None:
        assert {member.value for member in GovernanceAction} == {
            "mutation_refused",
            "mutation_denied",
            "policy_undefined",
            "change_set_blocked",
            "approval_required",
            "change_set_auto_approved",
            "change_set_approved",
            # Added with `GovernanceChokepoint.reject`. §3.6 has always defined
            # `pending_approval → rejected`, and revision 0010's CHECK constraint has always
            # permitted the state, but nothing implemented the edge: the old `approvals/routes.py`
            # mutated an in-process dict, so a human refusal produced no row and no audit record.
            # A refusal is at least as auditable as an approval — "who stopped this, and why" is
            # the question asked after an incident — so it gets its own action rather than being
            # folded into `change_set_approved` with an outcome flag, which would make "how often
            # do reviewers refuse" unanswerable.
            "change_set_rejected",
            "change_set_revert_authorised",
        }

    def test_a_generated_nonce_is_one_hundred_and_twenty_eight_bits_of_hex(self) -> None:
        """The spelling the committed fixture corpus uses, so one logical nonce has one form."""
        nonce = generate_nonce()
        assert len(nonce) == NONCE_HEX_LENGTH == 32
        assert int(nonce, 16) >= 0
        assert nonce == nonce.lower()

    def test_the_sequencer_protocol_names_both_replay_conditions(self) -> None:
        """§7.6 lists ordering and uniqueness as independent conditions; folding them into one
        call would make "the nonce was fresh" and "the sequence advanced" a single outcome."""
        assert hasattr(EnvelopeSequencer, "next_seq")
        assert hasattr(EnvelopeSequencer, "reserve_nonce")
