# SPDX-License-Identifier: FSL-1.1-ALv2
"""Endpoints an operator defined, merged into the routing configuration that ships in the image.

WHY A MERGE RATHER THAN A SECOND ROUTING PATH. `config/model-tiers.yaml` is the shipped cascade, and the
router, the breakers, the semantic cache and the availability surface all read `TierConfig`. An
operator-defined endpoint that lived anywhere else would need its own copy of each of those, and would be
excluded from the cascade — so it could be configured, tested, and still never serve a request. Merging into
`TierConfig` means a custom endpoint is a first-class member of a tier: it gets a breaker, it participates in
failover, and it appears on the Models screen alongside the rest.

WHERE IT IS PLACED IN THE CHAIN. Appended to the named tier's `self_hosted` list, never made `primary`.
A custom endpoint is tried after the shipped ones rather than in front of them, so adding one cannot silently
divert traffic away from a working configuration — an operator who wants it preferred can remove the ones
above it, which is a visible act.
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .provider_models import ProviderEndpoint
from .routing.tiers import EndpointDescriptor, EndpointProtocol, ModelTier, TierConfig


async def load_custom_endpoints(session: AsyncSession) -> list[ProviderEndpoint]:
    """Every operator-defined endpoint, in a stable order."""
    rows = (await session.execute(select(ProviderEndpoint).order_by(ProviderEndpoint.id))).scalars().all()
    return list(rows)


def merge_custom_endpoints(config: TierConfig, custom: list[ProviderEndpoint]) -> TierConfig:
    """Return a config with the operator's endpoints added to their tiers.

    A NEW CONFIG RATHER THAN A MUTATION. `TierConfig` is what Q-27 asserts provenance against, and a
    structure mutated after load cannot be compared with the file it came from. Building a new one keeps
    "what did we load" and "what are we running" separately answerable.

    An endpoint naming a tier that does not exist is SKIPPED rather than raising: the row is already
    committed, and refusing to compose the whole application because one row names a stale tier would turn a
    bad row into an outage. It is absent from routing and therefore absent from the Models screen, which is
    the visible consequence.
    """
    if not custom:
        return config

    endpoints = dict(config.endpoints)
    tiers = dict(config.tiers)

    for row in custom:
        try:
            tier = ModelTier(row.tier)
        except ValueError:
            continue
        chain = tiers.get(tier)
        if chain is None:
            continue

        endpoints[row.id] = EndpointDescriptor(
            id=row.id,
            provider="custom",
            model=row.model,
            protocol=EndpointProtocol(row.protocol),
            base_url=row.base_url,
            key_ref=row.key_ref,
            # `rank_source` is provenance, and "operator" is the honest value: this endpoint was not ranked
            # by any benchmark, it was named by a person. Claiming a rank it does not have would put it in
            # comparisons it has no business in.
            rank_source="operator",
        )
        if row.id not in chain.ordered_ids():
            tiers[tier] = replace(chain, self_hosted=(*chain.self_hosted, row.id))

    return TierConfig(tiers=tiers, endpoints=endpoints)
