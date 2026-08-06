# SPDX-License-Identifier: FSL-1.1-ALv2
"""Deploy-time secret injection as a governed operation (Leaf 10.5)."""

from typing import Mapping
from .models import Secret
from .store import SecretStore

async def inject_secrets(
    secrets: list[Secret], 
    store: SecretStore, 
    base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Retrieve secret values and inject them into a process environment.
    
    This is a governed operation and the ONLY module permitted to call
    `SecretStore.get_value()`.
    """
    env = dict(base_env) if base_env else {}
    for secret in secrets:
        # get_value is confined to this module via chokepoint_graph.py
        val = await store.get_value(secret)
        env[secret.key] = val
    return env
