# SPDX-License-Identifier: FSL-1.1-ALv2
"""Every cross-component call site binds against the real class (§0.4.2, §7.8).

Phase 0's D-23 defect in one assertion: the *caller's* argument shape must bind
against the *callee's* real signature. The MCP gateway called
`policy.filter_tools(server=…, tools=…, claims=…, blast_radius=…)` while the real
`OpaGatewayPolicy` implemented a different shape, and the doubles implemented the
caller's shape because they reassigned a `spec=`'d child. 419 tests stayed green
over a gateway that raised `TypeError` on every request.

This suite runs in milliseconds, needs no service, and fails the instant a
collaborator's signature drifts. The inventory it runs over is **derived by AST
scan**, never hand-written — see `scripts/collect_call_sites.py` for why.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from collect_call_sites import CallSite, collect_call_sites  # noqa: E402

pytestmark = pytest.mark.mandatory

#: Committed floor for the derived inventory. **This number may only be raised.**
#:
#: It is what stops a refactor from quietly emptying the inventory — the exact
#: failure mode that made Phase 0's coverage number meaningless. If a legitimate
#: change removes call sites, lowering this constant is a reviewable diff that has
#: to be argued for in the pull request, which is the point.
#:
#: 2026-07-30: 19 sites — 12 in the MCP gateway/routing graph, 5 in the model
#: router, 2 in the plan-analyzer pipeline.
INVENTORY_FLOOR = 19

_SITES = collect_call_sites()

#: A distinct object per parameter, so a signature that silently accepts anything
#: (e.g. `*args, **kwargs`) is still bound with the right *arity*.
_PLACEHOLDER = object()


def test_inventory_is_not_empty_and_grows_with_the_code() -> None:
    """A collector that silently returned [] would make this clause vacuous."""
    assert len(_SITES) >= INVENTORY_FLOOR, (
        f"the derived call-site inventory holds {len(_SITES)} sites but "
        f"INVENTORY_FLOOR is {INVENTORY_FLOOR}. Either a refactor removed call "
        "sites (justify it and lower the floor in the same commit) or "
        "scripts/collect_call_sites.py stopped recognising a binding form."
    )


def test_the_gateway_policy_site_is_still_covered() -> None:
    """The specific site D-23 was about must remain in the inventory.

    Without this, a change to the collector that stopped recognising constructor
    injection would drop the Phase 0 defect's own call site and the suite would
    still be green — vacuity by omission rather than by emptiness.
    """
    covered = {(s.target_dotted, s.method) for s in _SITES}
    assert ("src.mcp.policy.OpaGatewayPolicy", "filter_tools") in covered
    assert ("src.mcp.policy.OpaGatewayPolicy", "authorise_call") in covered


@pytest.mark.parametrize("site", _SITES, ids=str)
def test_call_site_binds_against_the_real_class(site: CallSite) -> None:
    """Resolve the collaborator, then bind the caller's shape to its signature."""
    target = site.resolve_target()

    attribute = getattr(target, site.method, None)
    assert attribute is not None, (
        f"{site.module}:{site.line} calls {site.target_dotted}.{site.method}(), "
        f"which does not exist on the real class. This is the D-23 failure mode: "
        "a caller and a callee that disagree, with a double in between agreeing "
        "with the caller."
    )
    assert callable(attribute), f"{site.target_dotted}.{site.method} is not callable"

    signature = inspect.signature(attribute)

    if site.has_star_args or site.has_star_kwargs:
        # The call unpacks a container, so the concrete arity is not visible in the
        # AST. Asserting existence is all this site can honestly prove; recording
        # the reason here rather than skipping keeps it counted in the selection.
        return

    # `getattr(cls, name)` yields the plain function, so `self` is part of the
    # signature; supply a placeholder for it.
    positional = [_PLACEHOLDER] * (site.positional_count + 1)
    keywords = {name: _PLACEHOLDER for name in site.keywords}

    try:
        signature.bind(*positional, **keywords)
    except TypeError as exc:
        pytest.fail(
            f"{site.module}:{site.line} calls "
            f"{site.target_dotted}.{site.method}({site.positional_count} positional, "
            f"keywords={list(site.keywords)}) but the real signature is "
            f"{site.method}{signature}: {exc}"
        )
