# SPDX-License-Identifier: FSL-1.1-ALv2
"""Environment rows and the sealing their secrets use.

WHY THE SEALING LIVES HERE AND NOT IN `integrations/github_link.py`.

The primitives are identical — HKDF from `ENVELOPE_PEPPER`, AES-256-GCM, the nonce in front of the
ciphertext — but the LABEL and the AAD are not, and those two are the whole security argument. The link
key is derived under `forgeops-github-link-v1` with a user id as the AAD; an environment secret is
derived under `forgeops-environment-secret-v1` with an environment id. Sharing the function and passing
an environment id as `user_id` would compile, work, and quietly mean that a ciphertext from one domain
opens in the other — so the two derivations stay separate and each says why.

THE AAD IS THE ENVIRONMENT ID, so a sealed value copied from staging's row into production's does not
open. That is the realistic accident: a support export, a partial restore, a hand-written UPDATE. Without
an AAD it would decrypt cleanly into the wrong environment and a production deployment would receive
staging's database password.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel

#: §2.1's four, plus `custom`. A closed set so "is this production?" is never a substring test.
ENVIRONMENT_KINDS: tuple[str, ...] = ("development", "test", "staging", "production", "custom")

#: The kinds that must require approval no matter what a request asks for. See
#: `EnvironmentService.create`: an operator may relax a staging environment, not a production one.
PROTECTED_KINDS: frozenset[str] = frozenset({"production"})

_SECRET_KEK_BYTES = 32
_SECRET_NONCE_BYTES = 12
_SECRET_LABEL = b"forgeops-environment-secret-v1"


def derive_secret_key(pepper: str | bytes) -> bytes:
    """The AES-256-GCM key-encryption key for environment secrets.

    An empty pepper is refused. Deriving from the empty string would produce a well-known key and seal
    every environment secret under it, which looks exactly like encryption and provides none — the same
    refusal `derive_link_key` makes, for the same reason.
    """
    material = pepper.encode("utf-8") if isinstance(pepper, str) else bytes(pepper)
    if not material:
        raise ValueError(
            "environment secret sealing requires a non-empty ENVELOPE_PEPPER: it is the input keying "
            "material for the secret key-encryption key"
        )
    return HKDF(algorithm=hashes.SHA256(), length=_SECRET_KEK_BYTES, salt=None, info=_SECRET_LABEL).derive(material)


def seal_secret(plaintext: str, *, environment_id: uuid.UUID, key: bytes) -> bytes:
    """`nonce || AES-256-GCM(key, nonce, plaintext, aad=environment_id.bytes)`.

    The caller cannot supply the nonce. The most damaging mistake available in this construction is a
    reused nonce, and the API simply does not offer it.
    """
    if len(key) != _SECRET_KEK_BYTES:
        raise ValueError(f"the sealing key must be {_SECRET_KEK_BYTES} bytes, got {len(key)}")
    nonce = os.urandom(_SECRET_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), environment_id.bytes)
    return nonce + ciphertext


def unseal_secret(sealed: bytes, *, environment_id: uuid.UUID, key: bytes) -> str:
    """Recover the value, with ONE message for every failure mode.

    Wrong key, wrong environment, truncated column, flipped bit — all the same message. The AEAD already
    refuses to say which, and re-deriving the distinction would hand anyone with database access an
    oracle for whether a transplanted ciphertext belongs to this deployment.
    """
    if len(key) != _SECRET_KEK_BYTES:
        raise ValueError(f"the sealing key must be {_SECRET_KEK_BYTES} bytes, got {len(key)}")
    raw = bytes(sealed)
    if len(raw) <= _SECRET_NONCE_BYTES:
        raise ValueError("the sealed environment secret could not be opened")
    try:
        opened = AESGCM(key).decrypt(raw[:_SECRET_NONCE_BYTES], raw[_SECRET_NONCE_BYTES:], environment_id.bytes)
    except Exception as exc:  # noqa: BLE001 - every AEAD failure is deliberately one message
        raise ValueError("the sealed environment secret could not be opened") from exc
    return opened.decode("utf-8")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class Environment(SQLModel, table=True):
    """One deployment target of a project. §2.1."""

    __tablename__ = "environments"
    __table_args__ = (
        CheckConstraint(_in_list("kind", ENVIRONMENT_KINDS), name="ck_environments_kind"),
        UniqueConstraint("project_id", "name", name="uq_environments_project_name"),
        # The promotion sequence must be TOTAL. Two environments at one position make "the next one"
        # a coin toss, and a promotion that picks arbitrarily is a deployment to the wrong place.
        UniqueConstraint("project_id", "position", name="uq_environments_project_position"),
        Index("ix_environments_project", "project_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="projects.id", ondelete="CASCADE")
    tenant_id: uuid.UUID | None = Field(default=None)
    name: str = Field(sa_column=Column("name", String(length=64), nullable=False))
    kind: str = Field(sa_column=Column("kind", String(length=32), nullable=False))
    k8s_context: str | None = Field(default=None, sa_column=Column("k8s_context", String(length=253), nullable=True))
    #: Whether a mutation targeting this environment needs a human.
    #:
    #: DEFAULTS TO TRUE. The safe direction is the default direction: an environment nobody has thought
    #: about yet behaves like production, and an operator opts out deliberately.
    requires_approval: bool = Field(
        default=True,
        sa_column=Column("requires_approval", Boolean, nullable=False, server_default=text("true")),
    )
    position: int = Field(sa_column=Column("position", Integer, nullable=False))
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )
    updated_at: datetime | None = Field(
        default=None,
        sa_column=Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )


class EnvironmentVariable(SQLModel, table=True):
    """One key of one environment. Sealed when `is_secret`, readable when not."""

    __tablename__ = "environment_variables"
    __table_args__ = (
        UniqueConstraint("environment_id", "key", name="uq_environment_variables_env_key"),
        # EXACTLY ONE COLUMN HOLDS THE VALUE, and the database says so. A row claiming `is_secret` with
        # a populated `value` would read as protected and be readable in clear; refusing it here means
        # no future writer can get the branch wrong quietly.
        CheckConstraint(
            "(is_secret AND value IS NULL AND value_sealed IS NOT NULL) OR "
            "(NOT is_secret AND value IS NOT NULL AND value_sealed IS NULL)",
            name="ck_environment_variables_exactly_one_value",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    environment_id: uuid.UUID = Field(
        sa_column=Column(
            "environment_id",
            ForeignKey("environments.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    key: str = Field(sa_column=Column("key", String(length=128), nullable=False))
    value: str | None = Field(default=None, sa_column=Column("value", Text, nullable=True))
    value_sealed: bytes | None = Field(default=None, sa_column=Column("value_sealed", LargeBinary, nullable=True))
    is_secret: bool = Field(
        default=False,
        sa_column=Column("is_secret", Boolean, nullable=False, server_default=text("false")),
    )
    created_at: datetime | None = Field(
        default=None,
        sa_column=Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    )


__all__ = [
    "ENVIRONMENT_KINDS",
    "PROTECTED_KINDS",
    "Environment",
    "EnvironmentVariable",
    "derive_secret_key",
    "seal_secret",
    "unseal_secret",
]
