# SPDX-License-Identifier: FSL-1.1-ALv2
"""The shipped tier YAML is what a running backend loads (design.md §0.5 D1, §11.1).

Phase 0 defined `load_tier_config(path, env)` and never called it from production.
`config/model-tiers.yaml` was exercised only by test fixtures, and criterion 17's
six-tier cascade was assembled from those fixtures — so what was proven was that the
cascade logic works, not that the shipped file is what a running backend routes on.
`PROGRESS.md` recorded it as outstanding; §0.5 lists it as debt D1, load-bearing for
every §1.5 generation leaf.

These tests assert provenance against the **running app**: mutate a copy of the
committed YAML, point `MODEL_TIER_CONFIG_PATH` at the copy, rebuild through
`create_app()`, and observe that the app's tier set followed. A test that read the
file and compared it to itself would restate the bug rather than catch it.

The same file also carries the `@wires(...)` declarations for the six routing
components the lifespan now composes, so `test_wiring_coverage.py` stays satisfied
and none of them can arrive untested.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from .production_app import (
    UNREACHABLE_DATABASE_URL,
    UNREACHABLE_REDIS_URL,
    apply_committed_baseline_env,
)
from .wiring import wires

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED_TIER_YAML = REPO_ROOT / "backend" / "config" / "model-tiers.yaml"


def _committed_document() -> dict:
    return yaml.safe_load(COMMITTED_TIER_YAML.read_text(encoding="utf-8"))


async def _app_from(
    tier_yaml: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str] | None = None,
) -> Iterator[FastAPI]:
    """Build the app through the production factory against `tier_yaml`.

    `env` is applied AFTER `apply_committed_baseline_env`, which is the only order that
    works: the baseline replays every key from `.env.example`, so anything a caller sets
    beforehand is silently overwritten by the committed default. The L2 wiring tests below
    found that the hard way — they set `LLM_KEY_VOYAGE` first and watched it come back as
    `placeholder`.
    """
    from src.main import create_app

    apply_committed_baseline_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", UNREACHABLE_REDIS_URL)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MODEL_TIER_CONFIG_PATH", str(tier_yaml))
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    app = create_app()
    async with LifespanManager(app):
        yield app


@wires("tier_config", "endpoint_registry", "breakers", "model_router", "semantic_cache", "ai_deps")
class TestTheRunningAppLoadsTheConfiguredFile:
    """The six routing components the lifespan composes, driven through the app."""

    async def test_the_committed_file_is_the_default_provenance(self, production_app: FastAPI) -> None:
        """With no override, the app's tiers equal the committed document's tiers."""
        document = _committed_document()
        expected = set(document["tiers"])
        actual = {tier.value for tier in production_app.state.tier_config.tiers}
        assert actual == expected, f"app tiers {sorted(actual)} != file tiers {sorted(expected)}"

    async def test_all_six_tiers_are_present(self, production_app: FastAPI) -> None:
        """§1.5 sits on six-tier routing; a partial load must not look healthy."""
        assert len(production_app.state.tier_config.tiers) == 6, sorted(
            t.value for t in production_app.state.tier_config.tiers
        )

    async def test_removing_a_tier_from_the_file_removes_it_from_the_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The assertion D1 is about: the running app follows the file.

        A default fallback masking a load failure would keep all six tiers here.
        """
        document = _committed_document()
        removed = sorted(document["tiers"])[0]
        del document["tiers"][removed]

        copy = tmp_path / "model-tiers.yaml"
        copy.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")

        async for app in _app_from(copy, monkeypatch):
            actual = {tier.value for tier in app.state.tier_config.tiers}
            assert removed not in actual, f"{removed} survived its removal from the file"
            assert actual == set(document["tiers"])

    async def test_repointing_a_tiers_primary_endpoint_changes_the_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provenance holds for the chain contents, not only the tier names."""
        document = _committed_document()
        tier_name = sorted(document["tiers"])[0]
        chain = document["tiers"][tier_name]
        replacement = next(eid for eid in document["endpoints"] if eid != chain["primary"])
        chain["primary"] = replacement

        copy = tmp_path / "model-tiers.yaml"
        copy.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")

        async for app in _app_from(copy, monkeypatch):
            from src.ai.routing.tiers import ModelTier

            assert app.state.tier_config.tiers[ModelTier(tier_name)].primary == replacement

    async def test_the_endpoint_registry_is_built_from_the_same_document(self, production_app: FastAPI) -> None:
        """One document, one registry: a second source would let them disagree."""
        tier_config = production_app.state.tier_config
        registry = production_app.state.endpoint_registry
        for endpoint_id in tier_config.endpoints:
            assert registry.get_availability(endpoint_id) is not None, endpoint_id

    async def test_a_breaker_exists_for_every_configured_endpoint(self, production_app: FastAPI) -> None:
        assert set(production_app.state.breakers) == set(production_app.state.tier_config.endpoints)

    async def test_the_router_routes_on_the_loaded_config(self, production_app: FastAPI) -> None:
        """The router must hold the same object the app exposes, not a rebuild."""
        assert production_app.state.model_router._tier_config is production_app.state.tier_config


@wires("tier_config", "endpoint_registry", "breakers", "ai_deps")
class TestTheTiersRouteServesTheLoadedConfig:
    """`GET /api/v1/ai/tiers` over the real composition.

    This route was registered by Phase 0 and `app.state.ai_deps` was never set, so
    every request raised AttributeError while `PROGRESS.md` recorded the endpoint as
    live. Driving it here is what makes "live" mean something.

    Phase 1 made the route authenticated (§4.4 does not list it as public — the response
    names every configured endpoint, its protocol and its breaker state, which is a map
    of the deployment's model supply chain). The auth dependency is therefore overridden
    here rather than a token minted: the subject of these assertions is tier
    PROVENANCE, and the authentication requirement itself is asserted by
    `test_wiring_auth.py` over the same composed app and by Q-19 over the whole router.
    """

    @staticmethod
    def _authorise(app: FastAPI) -> None:
        import uuid as _uuid

        from src.auth.dependencies import require_mcp_principal
        from src.auth.models import UserRole
        from src.auth.principal import Principal

        principal = Principal.for_user(
            user_id=_uuid.uuid4(),
            subject="test-only-not-a-real-subject",
            email="tiers@example.invalid",
            role=UserRole.DEVELOPER,
        )
        app.dependency_overrides[require_mcp_principal] = lambda: principal

    async def test_the_route_answers_from_the_loaded_tier_set(self, production_app: FastAPI) -> None:
        import httpx

        self._authorise(production_app)
        transport = httpx.ASGITransport(app=production_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/ai/tiers")

        assert response.status_code == 200, response.text
        names = {tier["name"] for tier in response.json()["tiers"]}
        assert names == {t.value for t in production_app.state.tier_config.tiers}

    async def test_the_route_reports_every_tiers_primary_endpoint(self, production_app: FastAPI) -> None:
        import httpx

        self._authorise(production_app)
        transport = httpx.ASGITransport(app=production_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/v1/ai/tiers")

        by_name = {tier["name"]: tier for tier in response.json()["tiers"]}
        for tier, chain in production_app.state.tier_config.tiers.items():
            assert by_name[tier.value]["primary_endpoint"] == chain.primary


class TestTheSemanticCacheIsWiredForL2:
    """Criterion 14's L2 tier must be CONSTRUCTED, not merely implemented.

    This class exists because of finding 79. `ai/routing/cache.py` grew a real L2 tier —
    per-model vector index, cosine similarity, inclusive threshold — and five passing
    integration tests, and `create_app` went on building the cache as
    `TieredSemanticCache(redis=redis_client)`. L2 was therefore unreachable in the running
    backend and `settings.semantic_cache_threshold` was read by nothing, while `PROGRESS.md`
    recorded criterion 14 as met. A tested capability with an untested construction site is
    not a shipped feature.

    Every assertion below reads the CONSTRUCTED object out of `app.state`, never a mock's
    call arguments. A refactor that stopped passing the embedder would satisfy a call-args
    assertion by simply not being called; it cannot satisfy these.
    """

    #: A Voyage key that is not the `.env.example` placeholder.
    #:
    #: `EmbeddingOrchestrator` treats the literal `placeholder` as "no key" and answers from
    #: an input-INDEPENDENT fallback vector, so `_build_cache_embedder` refuses to enable L2
    #: over it. Any other value counts as configured. Nothing here contacts Voyage: these
    #: tests assert on composition and perform no lookup.
    L2_ENABLED_ENV = {
        "EMBEDDING_BACKEND": "voyage",
        "LLM_KEY_VOYAGE": "test-only-not-a-real-voyage-key",
    }

    async def test_a_configured_embedder_is_actually_passed_to_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The assertion finding 79 needed and did not have.

        Comment out the `embed=` argument in `create_app` and this fails.
        """
        shutil.copyfile(COMMITTED_TIER_YAML, tmp_path / "model-tiers.yaml")

        async for app in _app_from(tmp_path / "model-tiers.yaml", monkeypatch, env=self.L2_ENABLED_ENV):
            cache = app.state.semantic_cache
            assert cache._embed is not None, (
                "create_app built the semantic cache WITHOUT an embedder while an embedding "
                "backend was configured, so criterion 14's L2 tier is unreachable at runtime "
                "-- this is finding 79 recurring"
            )
            assert callable(cache._embed)

    async def test_the_threshold_comes_from_settings_not_a_literal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-default value must reach the constructed cache.

        Asserting against 0.95 would pass whether the setting was read or the default was
        re-literalled, which is exactly how the unread setting survived. So the environment
        is set to a value the default cannot produce.
        """
        shutil.copyfile(COMMITTED_TIER_YAML, tmp_path / "model-tiers.yaml")
        env = {**self.L2_ENABLED_ENV, "SEMANTIC_CACHE_THRESHOLD": "0.83"}

        async for app in _app_from(tmp_path / "model-tiers.yaml", monkeypatch, env=env):
            assert app.state.semantic_cache._threshold == pytest.approx(0.83), (
                "the cache's threshold did not follow SEMANTIC_CACHE_THRESHOLD, so it is a "
                "literal rather than configuration"
            )

    async def test_the_embedder_the_cache_holds_is_input_sensitive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L2 over an input-independent embedder would serve any prompt from any entry.

        `EmbeddingOrchestrator`'s fallback is `[0.01 * (i % 100) for i in range(1024)]`,
        which ignores its argument: two unrelated prompts embed identically, cosine
        similarity is 1.0, and every prompt becomes a near-duplicate of every other. So the
        wiring must refuse that path, and this asserts the refusal rather than trusting it.
        """
        shutil.copyfile(COMMITTED_TIER_YAML, tmp_path / "model-tiers.yaml")
        env = {"EMBEDDING_BACKEND": "voyage", "LLM_KEY_VOYAGE": "placeholder"}

        async for app in _app_from(tmp_path / "model-tiers.yaml", monkeypatch, env=env):
            assert app.state.semantic_cache._embed is None, (
                "L2 was enabled over the placeholder key, whose embedding fallback ignores "
                "its input -- the cache would serve an arbitrary completion for any prompt"
            )

    async def test_an_unconfigured_backend_leaves_the_cache_l1_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L2 is optional by design: no embedder means exact-match, not a startup failure."""
        shutil.copyfile(COMMITTED_TIER_YAML, tmp_path / "model-tiers.yaml")

        async for app in _app_from(tmp_path / "model-tiers.yaml", monkeypatch, env={"EMBEDDING_BACKEND": "bge_m3"}):
            cache = app.state.semantic_cache
            assert cache is not None, "no cache was constructed at all"
            assert cache._embed is None


class TestALoadFailureIsNotMasked:
    """No default fallback may stand in for a file that failed to load."""

    async def test_a_missing_file_fails_startup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing = tmp_path / "absent.yaml"
        with pytest.raises((FileNotFoundError, OSError)):
            async for _ in _app_from(missing, monkeypatch):
                pytest.fail("startup succeeded with no tier file")

    async def test_a_malformed_file_fails_startup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        broken = tmp_path / "model-tiers.yaml"
        broken.write_text("tiers: {}\n", encoding="utf-8")  # no `endpoints` key
        with pytest.raises(ValueError):
            async for _ in _app_from(broken, monkeypatch):
                pytest.fail("startup succeeded with a malformed tier file")

    async def test_an_unset_base_url_variable_fails_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 0 shipped the literal `${OPENAI_BASE_URL}/...` to httpx.

        `load_tier_config` refuses an unexpandable placeholder, and that refusal has
        to reach startup rather than being swallowed into a degraded default.
        """
        copy = tmp_path / "model-tiers.yaml"
        shutil.copyfile(COMMITTED_TIER_YAML, copy)

        from src.main import create_app

        apply_committed_baseline_env(monkeypatch)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
        monkeypatch.setenv("REDIS_URL", UNREACHABLE_REDIS_URL)
        monkeypatch.setenv("MODEL_TIER_CONFIG_PATH", str(copy))

        with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
            app = create_app()
            async with LifespanManager(app):
                pytest.fail("startup succeeded with an unset base_url variable")
