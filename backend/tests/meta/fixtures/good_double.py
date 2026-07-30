# SPDX-License-Identifier: FSL-1.1-ALv2
"""POSITIVE fixture for `scripts/check-test-doubles.py`. Every double here is correct.

Paired with `bad_double.py`: the meta test asserts this file yields **zero**
findings. Without it, a lint that flagged everything unconditionally would also
pass its negative test, which would prove nothing.

The correct patterns are:

* `create_autospec(Cls, spec_set=True, instance=True)` — children carry the real
  method signatures, and assigning over a child raises instead of silently
  discarding enforcement;
* configure behaviour on the child (`.return_value`, `.side_effect`), never by
  assigning over it;
* `patch(..., autospec=True)` for a project-owned target;
* a suppression that carries a reason.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec, patch

from src.mcp.policy import OpaGatewayPolicy


def good_autospec_with_spec_set() -> object:
    """The prescribed form: autospec plus spec_set, configured on the child."""
    policy = create_autospec(OpaGatewayPolicy, spec_set=True, instance=True)
    policy.filter_tools.return_value = []
    policy.authorise_call.side_effect = None
    return policy


def good_patch_with_autospec() -> object:
    """A project-owned patch target, autospec'd."""
    return patch("src.mcp.policy.OpaGatewayPolicy.filter_tools", autospec=True)


def good_patch_object_with_autospec() -> object:
    """`patch.object` is fine when autospec'd."""
    return patch.object(OpaGatewayPolicy, "authorise_call", autospec=True)


def good_third_party_patch_needs_no_autospec() -> object:
    """Not project-owned: pinning httpx's signature is the lockfile's job."""
    return patch("httpx.AsyncClient.post")


def good_suppression_carries_a_reason() -> object:
    """A suppression with a stated reason is accepted; a bare one is FO-TD001."""
    policy = create_autospec(OpaGatewayPolicy, spec_set=True, instance=True)
    # This IS a genuine FO-TD001 reassignment over a spec'd child, so the rule
    # fires and the suppression is actually exercised. Suppressed WITH a reason,
    # which is the only accepted form, so the file must still yield no findings.
    policy.filter_tools = AsyncMock()  # noqa: FO-TD001 - fixture proving reasoned suppression is honoured
    return policy
