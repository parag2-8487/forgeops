# SPDX-License-Identifier: FSL-1.1-ALv2
"""BYO-key resolvers — resolve API keys from environment (Design §13.5).

Secret values are wrapped to prevent accidental logging.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


class SecretValue:
    """Wraps a secret string to prevent accidental repr/logging exposure."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        """Explicitly access the underlying secret."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


@runtime_checkable
class KeyResolver(Protocol):
    """Protocol for resolving API keys by provider reference."""

    def resolve(self, key_ref: str) -> SecretValue | None:
        """Resolve a key reference to a SecretValue, or None if unavailable."""
        ...


class EnvKeyResolver:
    """Resolves API keys from environment variables.

    Convention: LLM_KEY_{PROVIDER_UPPER}, e.g. LLM_KEY_OPENAI, LLM_KEY_ANTHROPIC.
    """

    def __init__(self, *, prefix: str = "LLM_KEY_") -> None:
        self._prefix = prefix

    def resolve(self, key_ref: str) -> SecretValue | None:
        """Look up LLM_KEY_{key_ref.upper()} from environment."""
        env_var = f"{self._prefix}{key_ref.upper()}"
        value = os.environ.get(env_var)
        if value is None or value.strip() == "":
            return None
        return SecretValue(value.strip())
