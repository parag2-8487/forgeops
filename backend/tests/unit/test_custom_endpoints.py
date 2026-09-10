# SPDX-License-Identifier: FSL-1.1-ALv2
"""Operator-defined endpoints joining the shipped cascade.

WHY A MERGE. The router, the breakers, the semantic cache and the availability surface all read `TierConfig`.
An endpoint held anywhere else would need its own copy of each and would be excluded from failover — so it
could be configured, tested, and still never serve a request.

WHERE IT LANDS. Appended to the tier's `self_hosted` list, never made `primary`, so adding one cannot
silently divert traffic away from a configuration that already works.
"""

from __future__ import annotations

from src.ai.custom_endpoints import merge_custom_endpoints
from src.ai.provider_models import SUPPORTED_CUSTOM_PROTOCOL, ProviderEndpoint
from src.ai.routing.tiers import EndpointDescriptor, EndpointProtocol, ModelTier, TierChain, TierConfig


def a_config() -> TierConfig:
    """A two-tier shipped config, standing in for `config/model-tiers.yaml`."""
    endpoints = {
        "shipped-primary": EndpointDescriptor(
            id="shipped-primary",
            provider="openai",
            model="shipped-model",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="https://api.openai.invalid/v1",
            key_ref="openai",
        ),
        "shipped-local": EndpointDescriptor(
            id="shipped-local",
            provider="self_hosted",
            model="local-model",
            protocol=EndpointProtocol.OPENAI_COMPATIBLE,
            base_url="http://ollama:11434/v1",
            key_ref=None,
        ),
    }
    tiers = {
        ModelTier.HIGH_CODING: TierChain(primary="shipped-primary", self_hosted=("shipped-local",)),
        ModelTier.SELF_HOSTED: TierChain(primary="shipped-local"),
    }
    return TierConfig(tiers=tiers, endpoints=endpoints)


def a_row(
    *,
    endpoint_id: str = "my-endpoint",
    tier: str = "high_coding",
    key_ref: str | None = None,
    protocol: str = SUPPORTED_CUSTOM_PROTOCOL,
) -> ProviderEndpoint:
    return ProviderEndpoint(
        id=endpoint_id,
        model="my-model",
        base_url="http://my-host:8000/v1",
        protocol=protocol,
        key_ref=key_ref,
        tier=tier,
    )


class TestTheMerge:
    def test_no_rows_returns_the_configuration_untouched(self):
        config = a_config()
        assert merge_custom_endpoints(config, []) is config

    def test_it_returns_a_new_configuration_rather_than_mutating_the_loaded_one(self):
        """Q-27 asserts provenance against `TierConfig`.

        A structure mutated after load cannot be compared with the file it came from, so "what did we load"
        and "what are we running" stay separately answerable.
        """
        config = a_config()
        merged = merge_custom_endpoints(config, [a_row()])

        assert merged is not config
        assert "my-endpoint" in merged.endpoints
        assert "my-endpoint" not in config.endpoints, "the original must be unchanged"
        assert "my-endpoint" not in config.tiers[ModelTier.HIGH_CODING].self_hosted

    def test_the_endpoint_joins_the_tiers_self_hosted_fallbacks(self):
        merged = merge_custom_endpoints(a_config(), [a_row()])
        chain = merged.tiers[ModelTier.HIGH_CODING]

        assert "my-endpoint" in chain.self_hosted

    def test_it_never_becomes_the_tiers_primary(self):
        """Adding an endpoint must not silently redirect traffic away from a working one.

        An operator who wants it preferred can remove the ones above it, which is a visible act.
        """
        merged = merge_custom_endpoints(a_config(), [a_row()])

        assert merged.tiers[ModelTier.HIGH_CODING].primary == "shipped-primary"

    def test_it_is_appended_after_the_shipped_fallbacks_rather_than_in_front(self):
        merged = merge_custom_endpoints(a_config(), [a_row()])
        chain = merged.tiers[ModelTier.HIGH_CODING]

        assert chain.self_hosted == ("shipped-local", "my-endpoint")

    def test_the_shipped_endpoints_all_survive(self):
        merged = merge_custom_endpoints(a_config(), [a_row()])

        assert "shipped-primary" in merged.endpoints
        assert "shipped-local" in merged.endpoints

    def test_it_records_the_operator_as_the_source_of_its_ranking(self):
        """Provenance. This endpoint was not ranked by any benchmark, it was named by a person.

        Claiming a rank it does not have would enter it into comparisons it has no business in.
        """
        merged = merge_custom_endpoints(a_config(), [a_row()])

        assert merged.endpoints["my-endpoint"].rank_source == "operator"

    def test_the_row_becomes_a_descriptor_the_router_can_use(self):
        merged = merge_custom_endpoints(a_config(), [a_row(key_ref="my-endpoint")])
        descriptor = merged.endpoints["my-endpoint"]

        assert descriptor.model == "my-model"
        assert descriptor.base_url == "http://my-host:8000/v1"
        assert descriptor.protocol == EndpointProtocol.OPENAI_COMPATIBLE
        assert descriptor.key_ref == "my-endpoint"

    def test_an_endpoint_needing_no_credential_carries_no_reference(self):
        merged = merge_custom_endpoints(a_config(), [a_row()])
        assert merged.endpoints["my-endpoint"].key_ref is None

    def test_several_rows_all_land_in_their_tiers(self):
        merged = merge_custom_endpoints(
            a_config(),
            [a_row(endpoint_id="one", tier="high_coding"), a_row(endpoint_id="two", tier="self_hosted")],
        )

        assert "one" in merged.tiers[ModelTier.HIGH_CODING].self_hosted
        assert "two" in merged.tiers[ModelTier.SELF_HOSTED].self_hosted

    def test_merging_the_same_row_twice_does_not_duplicate_it_in_the_chain(self):
        once = merge_custom_endpoints(a_config(), [a_row()])
        twice = merge_custom_endpoints(once, [a_row()])

        assert twice.tiers[ModelTier.HIGH_CODING].self_hosted.count("my-endpoint") == 1


class TestABadRowDoesNotTakeTheApplicationDown:
    def test_a_row_naming_a_tier_that_does_not_exist_is_skipped(self):
        """The row is already committed. Refusing to compose the application because one row names a stale
        tier would turn a bad row into an outage. It is absent from routing, and therefore absent from the
        Models screen, which is the visible consequence."""
        merged = merge_custom_endpoints(a_config(), [a_row(tier="no-such-tier")])

        assert "my-endpoint" not in merged.endpoints

    def test_a_row_naming_a_tier_absent_from_this_configuration_is_skipped(self):
        # `medium` is a real `ModelTier` but this config does not define a chain for it.
        merged = merge_custom_endpoints(a_config(), [a_row(tier="medium")])

        assert "my-endpoint" not in merged.endpoints

    def test_one_bad_row_does_not_stop_the_good_ones(self):
        merged = merge_custom_endpoints(
            a_config(),
            [a_row(endpoint_id="bad", tier="no-such-tier"), a_row(endpoint_id="good", tier="high_coding")],
        )

        assert "bad" not in merged.endpoints
        assert "good" in merged.endpoints
