import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .opa import PROJECT_PARAMETER_KEYS


def _validated_parameters(value: dict[str, Any]) -> dict[str, Any]:
    """Reject parameter keys the governance bundle does not read.

    REFUSED RATHER THAN DROPPED, and that is the whole point. `policies.opa._project_parameters`
    filters to `PROJECT_PARAMETER_KEYS` on the way to OPA, so an unknown key was already harmless —
    but harmless is not the same as honest. A caller who stores `blocked_days` (the bundle reads
    `blocked_weekdays`) would get a 201, see their policy listed as enabled, and have it restrict
    nothing, which is exactly the class of silent non-enforcement FR-32 was failing on. A 422 naming
    the accepted keys turns a typo into an error message instead of a false sense of protection.

    The type of each value is left to the bundle, which is the thing that reads it. Asserting shapes
    here would be a second schema to keep in step with the Rego, and the two would drift.
    """
    unknown = sorted(set(value) - set(PROJECT_PARAMETER_KEYS))
    if unknown:
        raise ValueError(
            f"unknown policy parameter(s): {', '.join(unknown)}. "
            f"The governance bundle reads only: {', '.join(PROJECT_PARAMETER_KEYS)}."
        )
    return value


class PolicyCreate(BaseModel):
    name: str = Field(..., max_length=200)
    engine: str = Field(default="rego", max_length=16)
    rego_rules: str
    enabled: bool = Field(default=True)
    template_id: str | None = Field(default=None, max_length=64)
    #: The values this policy's rules read (FR-32, FR-33). Validated against the closed set the bundle
    #: actually consults, so a caller cannot store a parameter that will be silently ignored and then
    #: reasonably believe their policy is in force.
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def _only_keys_the_bundle_reads(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validated_parameters(value)


class PolicyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    rego_rules: str | None = None
    enabled: bool | None = None
    template_id: str | None = Field(default=None, max_length=64)
    parameters: dict[str, Any] | None = None

    @field_validator("parameters")
    @classmethod
    def _only_keys_the_bundle_reads(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else _validated_parameters(value)


class PolicyRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID | None
    tenant_id: uuid.UUID | None
    name: str
    engine: str
    rego_rules: str
    enabled: bool
    template_id: str | None
    #: Returned so the screen can show what a policy actually restricts. A policy list that shows a
    #: name and an enabled flag cannot answer "which weekday does this block?", which is the only
    #: question a reader has.
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: Present so a list can be ordered and a stale row recognised. The columns have existed since
    #: revision `0005`; the response model simply never carried them, which is why a policy screen
    #: could not tell a rule written this morning from one written in July.
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PolicyPage(BaseModel):
    """A keyset page of policies, newest first.

    Shaped exactly like `ProjectPage` and `AuditPage`: a list plus the cursor that fetches the next
    page, `None` on the last one. Keyset over `(created_at, id)` rather than `OFFSET`, for the reason
    `projects/routes.py` gives — an offset shifts as rows are inserted, so a client paging while
    policies are authored sees duplicates or gaps.
    """

    policies: list[PolicyRead]
    next_cursor: str | None = None


class PolicyTemplateRead(BaseModel):
    id: str
    name: str
    description: str
    rego_rules: str
    parameters: dict[str, Any]


class DryRunInput(BaseModel):
    input: dict[str, Any]


class DryRunResult(BaseModel):
    """The outcome of a real OPA evaluation.

    `decision` used to be the whole body, and when the `opa` binary was absent the route
    SYNTHESISED one — `"allow" if input["action"] == "allow_me" else "deny"` — so a security surface
    answered with a verdict no policy engine had computed. There is now no path that returns this
    model without OPA having produced the value; a missing binary is `503 dryrun-unavailable`.

    `rule` and `evaluated_with` exist so the answer is attributable. A decision with no statement of
    what produced it is indistinguishable from the fabricated one it replaces.
    """

    decision: str
    #: The Rego query that was evaluated, verbatim.
    rule: str
    #: The evaluator's own version string, e.g. `opa 1.4.2`. Read from the binary, never assumed.
    evaluated_with: str
    #: True when OPA returned no value for the query at all — a policy that does not define the
    #: decision rule for this input. Distinct from an explicit `deny`, and the UI says so.
    undefined: bool = False
