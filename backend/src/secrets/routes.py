import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.auth.dependencies import require_principal
from src.auth.principal import Principal
from src.core.db import get_session

from .dependencies import get_secret_store
from .models import Secret
from .store import SecretStore

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])


class SecretCreate(BaseModel):
    project_id: uuid.UUID
    environment: str
    key: str
    value: str  # Only accepted here, not exposed on GET


class SecretUpdate(BaseModel):
    value: str


class SecretResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    environment: str
    key: str
    infisical_path: str | None
    is_local: bool


@router.post("", response_model=SecretResponse)
async def create_secret(
    payload: SecretCreate,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    store: SecretStore = Depends(get_secret_store),
):
    """Create a new secret metadata record and store value in backend."""
    secret = Secret(
        project_id=payload.project_id,
        environment=payload.environment,
        key=payload.key,
    )
    # This will set either encrypted_value or infisical_path on the secret.
    await store.set_value(secret, payload.value)

    session.add(secret)
    await session.commit()
    await session.refresh(secret)

    return SecretResponse(
        id=secret.id,
        project_id=secret.project_id,
        environment=secret.environment,
        key=secret.key,
        infisical_path=secret.infisical_path,
        is_local=secret.encrypted_value is not None,
    )


@router.get("", response_model=list[SecretResponse])
async def list_secrets(
    project_id: uuid.UUID,
    environment: str | None = None,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
):
    """Expose metadata only."""
    query = select(Secret).where(Secret.project_id == project_id)
    if environment:
        query = query.where(Secret.environment == environment)

    result = await session.execute(query)
    secrets = result.scalars().all()

    return [
        SecretResponse(
            id=s.id,
            project_id=s.project_id,
            environment=s.environment,
            key=s.key,
            infisical_path=s.infisical_path,
            is_local=s.encrypted_value is not None,
        )
        for s in secrets
    ]


@router.patch("/{secret_id}", response_model=SecretResponse)
async def update_secret(
    secret_id: uuid.UUID,
    payload: SecretUpdate,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
    store: SecretStore = Depends(get_secret_store),
):
    """Update a secret's value."""
    result = await session.execute(select(Secret).where(Secret.id == secret_id))
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")

    await store.set_value(secret, payload.value)
    await session.commit()
    await session.refresh(secret)

    return SecretResponse(
        id=secret.id,
        project_id=secret.project_id,
        environment=secret.environment,
        key=secret.key,
        infisical_path=secret.infisical_path,
        is_local=secret.encrypted_value is not None,
    )


@router.delete("/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    secret_id: uuid.UUID,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_session),
):
    """Delete a secret."""
    result = await session.execute(select(Secret).where(Secret.id == secret_id))
    secret = result.scalar_one_or_none()
    if not secret:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found")

    # THE METADATA RECORD GOES; THE MATERIAL IN INFISICAL DOES NOT, and that is a decision rather
    # than an unfinished edge. This platform does not own the Infisical project — it holds a machine
    # identity scoped to reading and writing paths — so deleting from it would be acting outside what
    # it manages, and a secret another system also injects would vanish without that system's owner
    # having agreed. A locally sealed secret has no such second owner, and its `encrypted_value` goes
    # with this row.
    #
    # The consequence is visible rather than implicit: the vault UI states it on the delete
    # confirmation, so an operator removing a reference knows what is left behind and where.
    await session.delete(secret)
    await session.commit()
