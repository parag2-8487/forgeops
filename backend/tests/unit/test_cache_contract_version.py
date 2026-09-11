# SPDX-License-Identifier: FSL-1.1-ALv2
"""The cache must not survive a correctness fix.

An entry stored before the gate learned a new rule was returned forever afterwards for an unchanged
repository, because the key was model + prompt + params and none of those change when the product's idea
of a correct answer changes. The improvement could not take effect, and the only way to see the new
behaviour was to delete `ai:cache:*` by hand - which had to be done twice while verifying fixes that had
already shipped.
"""

from __future__ import annotations

from src.ai.routing.cache import TieredSemanticCache
from src.ai.routing.contract_version import GENERATION_CONTRACT_VERSION


class _Redis:
    """Enough of the interface for `_build_key`, which touches none of it."""

    async def get(self, _key: str) -> None:  # pragma: no cover - not exercised here
        return None


def _cache() -> TieredSemanticCache:
    return TieredSemanticCache(redis=_Redis())


class TestTheContractVersionIsPartOfTheKey:
    def test_two_versions_of_the_contract_do_not_share_a_key(self) -> None:
        """The whole point: bumping the version must strand the old entries."""
        cache = _cache()
        prompt = "generate the deployment artifacts"

        key_now = cache._build_key("qwen2.5-coder:1.5b", prompt, None)

        import src.ai.routing.cache as cache_module

        original = cache_module.GENERATION_CONTRACT_VERSION
        try:
            cache_module.GENERATION_CONTRACT_VERSION = original + 1
            key_after_bump = cache._build_key("qwen2.5-coder:1.5b", prompt, None)
        finally:
            cache_module.GENERATION_CONTRACT_VERSION = original

        assert key_now != key_after_bump, (
            "the contract version is not in the cache key, so a correctness fix would be served "
            "the answer it was meant to replace"
        )

    def test_the_key_is_still_deterministic_for_one_contract(self) -> None:
        """Versioning must not make the cache useless: same inputs, same key."""
        cache = _cache()

        first = cache._build_key("m", "p", {"temperature": 0})
        second = cache._build_key("m", "p", {"temperature": 0})

        assert first == second

    def test_the_prompt_still_discriminates(self) -> None:
        cache = _cache()

        assert cache._build_key("m", "one", None) != cache._build_key("m", "two", None)

    def test_the_model_still_discriminates(self) -> None:
        """Two models answering the same prompt are two different answers."""
        cache = _cache()

        assert cache._build_key("small", "p", None) != cache._build_key("large", "p", None)

    def test_the_version_is_a_positive_integer(self) -> None:
        """A string or a hash here would make the bump unreviewable in a diff."""
        assert isinstance(GENERATION_CONTRACT_VERSION, int)
        assert GENERATION_CONTRACT_VERSION >= 1
