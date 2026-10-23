"""The container-registry credential, resolved at delivery. Phase 2 2.2.

Implements `governance.chokepoint.RegistryCredentialProvider`. It lives in `secrets/` and not in
`governance/` or `hostops/` for two reasons, the second found by a gate rather than by judgement:
the chokepoint must not learn where a credential is kept, and `check-chokepoint` refuses any other
domain importing `secrets.store` or `secrets.injection` at all. Secret retrieval is this domain's
alone, so a provider that retrieves one belongs to it.

The first attempt put this under `hostops/` and the parse-based check rejected it twice over: once
for reaching `get_value` outside `secrets.injection`, once for the import itself. Both refusals were
right, and the fix was to move the file rather than to add an exemption.

WHY THE SECRET STORE AND NOT A NEW TABLE. A registry token is a project secret in exactly the sense
`secrets` already means: per project, per environment, held in Infisical or the sealed local store,
never returned by a read route. A `registry_credentials` table would be a second place credentials
live, with its own rotation story and its own chance of being selected into a response by mistake.

WHY TWO KEYS. The username is not a credential and the token is, and they have different exposure:
the username appears in a progress line and in the agent's `docker login` argv, the token appears in
neither. Separate secrets mean the username can be read for a panel without unsealing anything.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .injection import inject_secrets
from .models import Secret
from .store import SecretStore

#: The two keys a project sets to push to a registry. Named with the product prefix so they cannot
#: collide with an application secret a project already has.
REGISTRY_USERNAME_KEY = "FORGEOPS_REGISTRY_USERNAME"
REGISTRY_TOKEN_KEY = "FORGEOPS_REGISTRY_TOKEN"

#: Where a registry credential is looked up. A registry is infrastructure shared across a project's
#: environments — the same registry receives a dev image and a prod image — so it is keyed at `prod` and
#: read from there for every push rather than duplicated four times. A project that genuinely needs a
#: different registry per environment sets a per-environment override, which is a Phase 3 concern and is
#: NOT quietly half-implemented here.
REGISTRY_CREDENTIAL_ENVIRONMENT = "prod"


class SecretStoreRegistryCredentials:
    """Reads the registry username and token for a project from the secret store."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    async def registry_credential_for(
        self, session: AsyncSession, *, project_id: uuid.UUID, registry: str
    ) -> tuple[str, str] | None:
        """Return `(username, token)`, or `None` when this project has no registry credential.

        `registry` is accepted and deliberately NOT used to select the secret. It is part of the Protocol
        because a per-registry credential is the obvious next requirement, and a signature that cannot
        express it would have to change when it arrives. Ignoring it today is honest; branching on it
        today with one credential in the store would be a lookup that silently always matched.

        `None` IS A REAL ANSWER, not a failure. A push to an unauthenticated registry — a local `registry:2`
        in a test, or a cluster-internal registry reached over the pod network — needs no credential, and
        the agent refuses only the incoherent combination of a named user with no secret. Returning a
        fabricated pair here would turn "you have not configured a registry credential" into "the registry
        rejected your credential", which sends an operator to the wrong system.
        """
        username_secret = await self._secret_row(session, project_id, REGISTRY_USERNAME_KEY)
        token_secret = await self._secret_row(session, project_id, REGISTRY_TOKEN_KEY)
        if username_secret is None or token_secret is None:
            # HALF A CREDENTIAL IS NOT A CREDENTIAL. A project with a username and no token would
            # otherwise produce a login attempt with an empty secret, which the registry rejects with an
            # authentication error — blaming the token rather than its absence.
            return None

        # UNSEALED THROUGH `inject_secrets`, WHICH IS THE ONLY MODULE ALLOWED TO. `check-chokepoint`
        # confines `SecretStore.get_value` to `secrets.injection` so that no route and no other domain can
        # reveal a value, and it caught this file calling the store directly on the first attempt. The
        # confinement is right and the fix is to use the sanctioned path, not to add an exemption: one
        # unseal site is what makes "which code can read a secret" answerable.
        unsealed = await inject_secrets([username_secret, token_secret], self._store)
        username = unsealed.get(REGISTRY_USERNAME_KEY, "")
        token = unsealed.get(REGISTRY_TOKEN_KEY, "")
        if not username.strip() or not token.strip():
            # A stored empty string is the same case: present in the table, useless at the registry.
            return None
        return username.strip(), token

    async def _secret_row(self, session: AsyncSession, project_id: uuid.UUID, key: str) -> Secret | None:
        """Load one secret row, or `None`.

        Raw SQL against the columns the store needs rather than a full ORM load, because this runs inside
        `_mint_and_sign` — on the delivery path of every image push — and an ORM identity-map load here
        would attach rows to a session that is about to commit a change set.
        """
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, project_id, tenant_id, environment, key, infisical_path, encrypted_value "
                        "FROM secrets WHERE project_id = :project_id AND environment = :environment "
                        "AND key = :key LIMIT 1"
                    ),
                    {
                        "project_id": str(project_id),
                        "environment": REGISTRY_CREDENTIAL_ENVIRONMENT,
                        "key": key,
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return Secret(
            id=row["id"],
            project_id=row["project_id"],
            tenant_id=row["tenant_id"],
            environment=row["environment"],
            key=row["key"],
            infisical_path=row["infisical_path"],
            encrypted_value=row["encrypted_value"],
        )
