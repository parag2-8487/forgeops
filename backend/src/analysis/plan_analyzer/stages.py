# SPDX-License-Identifier: FSL-1.1-ALv2
"""Syntax and Schema validation stages."""

from __future__ import annotations

from .models import Finding, PlanDocument, Severity, StageContext


class SyntaxStage:
    """Validates the basic structure and parsability of the plan document."""

    name = "syntax"

    async def run(self, doc: PlanDocument, ctx: StageContext) -> list[Finding]:
        findings: list[Finding] = []

        if not doc.raw:
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.FATAL,
                    code="EMPTY_PLAN",
                    message="Plan document is empty",
                )
            )
            return findings

        if not doc.format_version:
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.ERROR,
                    code="MISSING_FORMAT_VERSION",
                    message="Plan is missing format_version field",
                )
            )

        if not doc.terraform_version:
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.WARNING,
                    code="MISSING_TERRAFORM_VERSION",
                    message="Plan is missing terraform_version field",
                )
            )

        return findings


class SchemaStage:
    """Validates the plan document conforms to the expected schema shape."""

    name = "schema"

    async def run(self, doc: PlanDocument, ctx: StageContext) -> list[Finding]:
        findings: list[Finding] = []

        # Validate format version is supported
        if doc.format_version and not doc.format_version.startswith(("1.", "0.")):
            findings.append(
                Finding(
                    stage=self.name,
                    severity=Severity.WARNING,
                    code="UNSUPPORTED_FORMAT_VERSION",
                    message=f"Format version {doc.format_version} may not be fully supported",
                )
            )

        # Validate resource_changes structure
        for i, rc in enumerate(doc.resource_changes):
            if not isinstance(rc, dict):
                findings.append(
                    Finding(
                        stage=self.name,
                        severity=Severity.ERROR,
                        code="INVALID_RESOURCE_CHANGE",
                        message=f"resource_changes[{i}] is not an object",
                    )
                )
                continue

            if "address" not in rc:
                findings.append(
                    Finding(
                        stage=self.name,
                        severity=Severity.ERROR,
                        code="MISSING_ADDRESS",
                        message=f"resource_changes[{i}] is missing 'address' field",
                        resource=f"resource_changes[{i}]",
                    )
                )

            change = rc.get("change")
            if change is None:
                findings.append(
                    Finding(
                        stage=self.name,
                        severity=Severity.ERROR,
                        code="MISSING_CHANGE",
                        message=f"resource_changes[{i}] is missing 'change' field",
                        resource=rc.get("address"),
                    )
                )
            elif not isinstance(change, dict):
                findings.append(
                    Finding(
                        stage=self.name,
                        severity=Severity.ERROR,
                        code="INVALID_CHANGE",
                        message=f"resource_changes[{i}].change is not an object",
                        resource=rc.get("address"),
                    )
                )
            elif "actions" not in change:
                findings.append(
                    Finding(
                        stage=self.name,
                        severity=Severity.ERROR,
                        code="MISSING_ACTIONS",
                        message=f"resource_changes[{i}].change is missing 'actions' field",
                        resource=rc.get("address"),
                    )
                )

        return findings
