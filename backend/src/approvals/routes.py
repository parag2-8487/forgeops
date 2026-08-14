# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, Depends, HTTPException
from src.approvals.schemas import ChangeSetResponse
from src.approvals.service import ApprovalService, get_approval_service

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.get("", response_model=list[ChangeSetResponse])
def list_approvals(project_id: str | None = None, service: ApprovalService = Depends(get_approval_service)):
    return service.list_changesets(project_id=project_id)


@router.get("/{cs_id}", response_model=ChangeSetResponse)
def get_approval(cs_id: str, service: ApprovalService = Depends(get_approval_service)):
    cs = service.get_changeset(cs_id)
    if not cs:
        raise HTTPException(status_code=404, detail="ChangeSet not found")
    return cs


@router.post("/{cs_id}/approve", response_model=ChangeSetResponse)
def approve_changeset(cs_id: str, approver: str = "admin", service: ApprovalService = Depends(get_approval_service)):
    try:
        return service.approve_changeset(cs_id, approver=approver)
    except KeyError:
        raise HTTPException(status_code=404, detail="ChangeSet not found") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{cs_id}/reject", response_model=ChangeSetResponse)
def reject_changeset(cs_id: str, rejector: str = "admin", service: ApprovalService = Depends(get_approval_service)):
    try:
        return service.reject_changeset(cs_id, rejector=rejector)
    except KeyError:
        raise HTTPException(status_code=404, detail="ChangeSet not found") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{cs_id}/rollback", response_model=ChangeSetResponse)
def rollback_changeset(cs_id: str, service: ApprovalService = Depends(get_approval_service)):
    try:
        return service.rollback_changeset(cs_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ChangeSet not found") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
