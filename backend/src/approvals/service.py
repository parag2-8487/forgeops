# SPDX-License-Identifier: Apache-2.0
import uuid

from src.approvals.schemas import ApprovalStatus, ChangeSetResponse


class ApprovalService:
    def __init__(self):
        self._store: dict[str, ChangeSetResponse] = {}

    def create_changeset(self, project_id: str, summary: str, diff: str) -> ChangeSetResponse:
        cs_id = f"cs-{uuid.uuid4().hex[:8]}"
        cs = ChangeSetResponse(
            id=cs_id, project_id=project_id, summary=summary, status=ApprovalStatus.PENDING, diff=diff
        )
        self._store[cs_id] = cs
        return cs

    def get_changeset(self, cs_id: str) -> ChangeSetResponse | None:
        return self._store.get(cs_id)

    def list_changesets(self, project_id: str | None = None) -> list[ChangeSetResponse]:
        if project_id:
            return [cs for cs in self._store.values() if cs.project_id == project_id]
        return list(self._store.values())

    def approve_changeset(self, cs_id: str, approver: str) -> ChangeSetResponse:
        cs = self._store.get(cs_id)
        if not cs:
            raise KeyError(f"ChangeSet '{cs_id}' not found")
        if cs.status != ApprovalStatus.PENDING:
            raise ValueError(f"Cannot approve ChangeSet in state '{cs.status}'")

        cs.status = ApprovalStatus.APPROVED
        cs.approved_by = approver
        return cs

    def reject_changeset(self, cs_id: str, rejector: str) -> ChangeSetResponse:
        cs = self._store.get(cs_id)
        if not cs:
            raise KeyError(f"ChangeSet '{cs_id}' not found")
        if cs.status != ApprovalStatus.PENDING:
            raise ValueError(f"Cannot reject ChangeSet in state '{cs.status}'")

        cs.status = ApprovalStatus.REJECTED
        return cs

    def rollback_changeset(self, cs_id: str) -> ChangeSetResponse:
        cs = self._store.get(cs_id)
        if not cs:
            raise KeyError(f"ChangeSet '{cs_id}' not found")
        if cs.status not in (ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED):
            raise ValueError(f"Cannot rollback ChangeSet in state '{cs.status}'")

        cs.status = ApprovalStatus.ROLLED_BACK
        return cs


_approval_service = ApprovalService()


def get_approval_service() -> ApprovalService:
    return _approval_service
