# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-27 — tier configuration provenance (design.md §11.1, §11.5.4, Appendix B).

Property, universally quantified:

    For every valid tier YAML document written to a temporary path, the tier set on
    the app built by `create_app()` equals the parsed document — the app's routing
    configuration is derived from the file, not from a default.

Why this is a property and not an example. The failure D1 describes is not "one tier
is wrong"; it is "the file is not the source at all". A single fixture can be
satisfied by a hard-coded default that happens to match the committed YAML, which is
exactly the state Phase 0 shipped. Quantifying over generated documents removes that
possibility: no fixed default can agree with an arbitrary generated tier set.

Negative control (`mutations.toml` Q-27): hard-code the tier map in the lifespan and
ignore the configured path. The property must then fail.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED_TIER_YAML = REPO_ROOT / "backend" / "config" / "model-tiers.yaml"


def _tier_names() -> tuple[str, ...]:
    """The tier names, read from the enum rather than restated here.

    A hand-written list is how this property first failed: it contained
    `high_reasoning`, which is not a `ModelTier`, so every generated document was
    rejected by the loader and the failure looked like a provenance bug rather than a
    strategy bug. Deriving the list means the generator cannot disagree with the code
    it is testing.
    """
    from src.ai.routing.tiers import ModelTier

    return tuple(tier.value for tier in ModelTier)


TIER_NAMES = _tier_names()

#: Building the real app runs the lifespan, including its two best-effort dependency
#: probes, so each example costs seconds rather than milliseconds. The example count
#: is therefore deliberately small and the deadline disabled: what this property needs
#: is *variety of documents*, and a dozen distinct generated tier sets already makes a
#: fixed default impossible. Raising it buys nothing and would make the suite a
#: candidate for being switched off, which is how P-09 became decorative.
_PROVENANCE_SETTINGS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _committed_document() -> dict:
    return yaml.safe_load(COMMITTED_TIER_YAML.read_text(encoding="utf-8"))


@st.composite
def tier_documents(draw: st.DrawFn) -> dict:
    """A valid tier document built from the committed endpoint catalogue.

    The endpoints are reused rather than generated because `load_tier_config`
    validates protocol names, absolute base URLs and `${VAR}` expansion; inventing
    endpoints would mostly generate documents the loader rightly rejects, and the
    property is about the *tier map* reaching the app, not about loader validation
    (which `test_wiring_tier_config.py` covers directly).
    """
    committed = _committed_document()
    endpoint_ids = sorted(committed["endpoints"])

    chosen = draw(st.lists(st.sampled_from(TIER_NAMES), min_size=1, max_size=6, unique=True))
    tiers: dict[str, dict] = {}
    for name in chosen:
        primary = draw(st.sampled_from(endpoint_ids))
        chain: dict[str, object] = {"primary": primary}
        if draw(st.booleans()):
            secondary = draw(st.sampled_from(endpoint_ids))
            if secondary != primary:
                chain["secondary"] = secondary
        tiers[name] = chain

    return {"tiers": tiers, "endpoints": committed["endpoints"]}


def _app_tier_map(tier_yaml: Path) -> dict[str, str]:
    """`{tier name: primary endpoint}` on the app built through `create_app()`.

    Synchronous on purpose: hypothesis drives this, and wrapping each example in
    `asyncio.run` keeps the strategy plumbing out of the property's statement.

    The environment is rebuilt from the committed baseline on EVERY call rather than
    once in an autouse fixture. Hypothesis reuses a function-scoped fixture across all
    examples of one test, so a fixture-supplied environment is shared mutable state
    spanning dozens of `create_app()` calls — and several suites in this repository
    assign to `os.environ` directly rather than through monkeypatch. The result was a
    property that passed in isolation and reported a hypothesis FlakyFailure inside the
    full run. Building the environment per example removes the ordering dependency
    rather than hiding it behind a retry.
    """
    from asgi_lifespan import LifespanManager
    from src.core.config import load_project_dotenv
    from src.main import create_app

    from ..integration.production_app import UNREACHABLE_DATABASE_URL, UNREACHABLE_REDIS_URL

    environment = dict(load_project_dotenv((".env.example",)))
    environment["DATABASE_URL"] = UNREACHABLE_DATABASE_URL
    environment["REDIS_URL"] = UNREACHABLE_REDIS_URL
    environment["APP_ENV"] = "test"
    environment["MODEL_TIER_CONFIG_PATH"] = str(tier_yaml)

    async def _build() -> dict[str, str]:
        app = create_app()
        async with LifespanManager(app):
            return {tier.value: chain.primary for tier, chain in app.state.tier_config.tiers.items()}

    saved = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        return asyncio.run(_build())
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


@pytest.fixture(autouse=True)
def _isolate_tier_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no ambient `MODEL_TIER_CONFIG_PATH` leaks into an example.

    The rest of the environment is built per example inside `_app_tier_map`; only this
    one key needs clearing up front, because a value left behind by another suite would
    be silently overwritten and then restored to the wrong thing.
    """
    monkeypatch.delenv("MODEL_TIER_CONFIG_PATH", raising=False)


class TestQ27TierConfigurationProvenance:
    @_PROVENANCE_SETTINGS
    @given(document=tier_documents())
    def test_the_apps_tier_set_equals_the_parsed_document(
        self, document: dict, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """The property itself, over generated documents."""
        target = tmp_path_factory.mktemp("q27") / "model-tiers.yaml"
        target.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")

        observed = _app_tier_map(target)
        expected = {name: chain["primary"] for name, chain in document["tiers"].items()}

        assert observed == expected, (
            "the running app's tier map does not equal the parsed document. Either a "
            "default is masking the load, or the configured path is being ignored "
            "(design.md §0.5 debt D1)."
        )

    @_PROVENANCE_SETTINGS
    @given(document=tier_documents())
    def test_no_tier_appears_that_the_document_does_not_declare(
        self, document: dict, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A default fallback would show up here as an extra tier.

        Stated separately from the equality above because this is the direction that
        catches a *merge* with a built-in default rather than a wholesale replacement,
        and the two mutations are not the same mistake.
        """
        target = tmp_path_factory.mktemp("q27") / "model-tiers.yaml"
        target.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")

        observed = set(_app_tier_map(target))
        assert observed <= set(document["tiers"]), sorted(observed - set(document["tiers"]))

    def test_the_generator_produces_documents_the_committed_file_does_not_match(self) -> None:
        """Non-vacuity guard for the strategy itself.

        If the generator only ever produced the committed tier map, the property would
        pass against a hard-coded default and prove nothing. This asserts the
        generator can disagree with the committed file, which is what makes the
        quantification meaningful.
        """
        committed = {name: chain["primary"] for name, chain in _committed_document()["tiers"].items()}
        examples: list[dict] = []
        find_disagreement = tier_documents().filter(
            lambda doc: {n: c["primary"] for n, c in doc["tiers"].items()} != committed
        )

        @settings(max_examples=25, deadline=None)
        @given(doc=find_disagreement)
        def _collect(doc: dict) -> None:
            examples.append(doc)

        _collect()
        assert examples, "the strategy never generated a document differing from the committed file"
