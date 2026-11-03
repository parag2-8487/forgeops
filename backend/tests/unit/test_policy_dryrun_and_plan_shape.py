# SPDX-License-Identifier: FSL-1.1-ALv2
"""Policy dry-run queries the policy's own package; malformed plans are a 422, not a 500 (2026-11-02)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from src.analysis.plan_analyzer.models import PlanDocument
from src.policies.routes import DECISION_QUERY, decision_query_for
from src.policies.templates import TEMPLATES


class TestDryRunQuery:
    def test_template_policies_are_queried_in_their_own_package(self) -> None:
        for template in TEMPLATES:
            query = decision_query_for(template.rego_rules)
            assert query.startswith("data.forgeops.policy.") and query.endswith(".decision"), template.id
            assert query != DECISION_QUERY

    def test_a_policy_in_the_bundle_package_keeps_the_bundle_query(self) -> None:
        assert decision_query_for('package forgeops.governance\n\ndecision := "allow"\n') == DECISION_QUERY

    def test_rego_without_a_package_falls_back_rather_than_guessing(self) -> None:
        assert decision_query_for('decision := "allow"') == DECISION_QUERY

    @pytest.mark.skipif(shutil.which("opa") is None, reason="opa binary not installed on this host")
    def test_every_template_now_answers_instead_of_undefined(self) -> None:
        """The reported symptom: every template-based policy dry-ran as `undefined`."""
        for template in TEMPLATES:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "p.rego"
                path.write_text(template.rego_rules, encoding="utf-8")
                out = subprocess.run(
                    ["opa", "eval", "-d", str(path), "-I", "-f", "json", decision_query_for(template.rego_rules)],
                    input=json.dumps({}),
                    capture_output=True,
                    text=True,
                    check=True,
                )
            assert json.loads(out.stdout).get("result"), f"{template.id} still evaluates to undefined"


class TestPlanShapeIsValidated:
    @pytest.mark.parametrize(
        ("plan", "message"),
        [
            ({"resource_changes": "not-a-list"}, "resource_changes must be a list"),
            ({"resource_changes": ["x"]}, "resource_changes[0] must be an object"),
            ({"resource_changes": [{"change": "x"}]}, "resource_changes[0].change must be an object"),
            ({"resource_changes": [{"change": {"actions": "delete"}}]}, "change.actions must be a list"),
        ],
    )
    def test_malformed_structures_are_rejected_with_the_field_named(self, plan: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):
            PlanDocument.from_json(json.dumps({"format_version": "1.2", **plan}))

    def test_a_well_formed_plan_still_parses(self) -> None:
        doc = PlanDocument.from_json(
            json.dumps(
                {"format_version": "1.2", "resource_changes": [{"address": "a", "change": {"actions": ["create"]}}]}
            )
        )
        assert len(doc.resource_changes) == 1
