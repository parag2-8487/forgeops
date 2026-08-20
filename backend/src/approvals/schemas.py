# SPDX-License-Identifier: FSL-1.1-ALv2
"""The approvals read surface's wire shapes (design.md §3.6, §6.5 revisions `0004`/`0010`).

These are a rewrite, and what they replace matters more than what they are.

The previous version of this module defined its own `ApprovalStatus` enum with the members
`PENDING`, `APPROVED`, `REJECTED`, `EXECUTED` and `ROLLED_BACK`, and a `ChangeSetResponse` whose
`id` and `project_id` were strings carrying a `summary` and a `diff`. None of that corresponded to
anything stored: revision `0004` created `change_sets`, `change_items`, `validations`, `approvals`
and `rollback_handles`; `src/governance/models.py` maps them; and revision `0010` put a CHECK
constraint on `change_sets.status` generated from `CHANGE_SET_STATUSES` — thirteen lowercase states
from §3.6. **Not one of the five uppercase names above is in that list**, so a row written with any
of them would have been refused by the database. It never was, because the store was a dict.

So this is not a cosmetic change of casing. Two vocabularies for one concept existed, one enforced
by Postgres and one enforced by nothing, and the unenforced one is the one the HTTP surface spoke.
The enum is deleted rather than mapped: a translation table between them would preserve the second
vocabulary and leave the next reader to discover which is authoritative.

`diff` is likewise gone as a single string. A change set's diff IS its `change_items` — each with a
path, an action, and content hashes before and after — and flattening that into one blob is what
made the old shape unable to express the two view modes §12.6 step 8 asks for, or the per-file
hashes step 10 verifies on disk.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ChangeItemRead(BaseModel):
    """One file's worth of a change set, from `change_items`.

    `old_content` and `new_content` are included because the diff viewer needs them and the
    agent's apply is verified against them. `old_hash`/`new_hash` are carried separately rather
    than recomputed by the client: §12.6 step 10 asserts the on-disk result against the hash the
    backend recorded, and a client-computed hash would be checking the client against itself.
    """

    id: uuid.UUID
    file_path: str
    action: str = Field(description="One of create, update, delete (CHANGE_ITEM_ACTIONS).")
    old_content: str | None = None
    new_content: str | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    ordinal: int


class ApprovalRead(BaseModel):
    """A human decision, from `approvals`.

    `approver_id` is a `users.id`, not a name — the column is a foreign key with `ON DELETE
    RESTRICT`, so a decision cannot be attributed to a user who does not exist, and the user
    behind a decision cannot be deleted. The old surface took the approver as a query-string
    string, which could name anybody.
    """

    id: uuid.UUID
    approver_id: uuid.UUID
    status: str = Field(description="One of approved, rejected (APPROVAL_STATUSES).")
    comment: str | None = None
    created_at: datetime


class ChangeSetSummary(BaseModel):
    """A change set without its items, for the list view."""

    id: uuid.UUID
    project_id: uuid.UUID
    status: str = Field(description="One of §3.6's thirteen states (CHANGE_SET_STATUSES).")
    origin: str
    blast_radius_score: int
    blast_radius_verdict: str
    #: The optimistic-concurrency token. Exposed deliberately: a client that displays a change set
    #: and later approves it should send the version it displayed, so a stale tab produces a 409
    #: rather than approving something that has since moved.
    version: int
    generation_run_id: uuid.UUID | None = None
    created_at: datetime
    applied_at: datetime | None = None


class ChangeSetDetail(ChangeSetSummary):
    """A change set with the items that constitute its diff and the decisions taken on it."""

    items: list[ChangeItemRead]
    approvals: list[ApprovalRead]


class ChangeSetPage(BaseModel):
    """A page of change sets plus the cursor that fetches the next one.

    Keyset rather than offset, matching the audit surface: an offset shifts under insertion, so a
    reviewer paging through pending change sets while new ones arrive would see rows twice or not
    at all.
    """

    change_sets: list[ChangeSetSummary]
    next_cursor: str | None = None


class ApprovalDecisionRequest(BaseModel):
    """The body of an approve or reject.

    A **body**, not query parameters, and this is the point of the rewrite. The old handlers took
    `approver: str = "admin"` and `rejector: str = "admin"` in the query string, so the caller
    supplied the identity that the audit record would attribute the decision to — and defaulted it
    to an administrator. The approver is now taken from the verified principal and cannot be
    expressed on the wire at all; there is deliberately no `approver` field here.

    `comment` carries the reviewer's reason into `approvals.comment` and into the audit record's
    `reason`. `expected_version` is the change set's `version` as the client last saw it.
    """

    comment: str | None = Field(default=None, max_length=4096)
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The version the client displayed. When supplied, a change set that has moved on "
            "produces 409 change-set-conflict instead of a decision on stale state."
        ),
    )
