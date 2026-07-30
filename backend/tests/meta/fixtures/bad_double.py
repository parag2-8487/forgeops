# SPDX-License-Identifier: FSL-1.1-ALv2
"""NEGATIVE fixture for `scripts/check-test-doubles.py`. Every double here is wrong.

This file is never executed and never collected by pytest. It exists so
`tests/meta/test_check_test_doubles.py` can assert that each rule actually fires:
a lint whose failure path has never fired is not a lint.

`scripts/check-test-doubles.py` excludes `tests/meta/fixtures/**` from its ordinary
walk — otherwise this file would keep the real tree permanently red and the check
would get switched off — and the meta test feeds it to `check_file` directly.

Expected findings, one per marked line:

    FO-TD001  reassignment over a spec'd child      (the Phase 0 D-23 defect)
    FO-TD002  spec= / create_autospec without spec_set=True
    FO-TD003  patch / patch.object without autospec=True on a project-owned target
    FO-TD001  a reasonless `# noqa` suppression
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, create_autospec, patch

from src.mcp.policy import OpaGatewayPolicy


def bad_reassignment_over_a_specd_child() -> AsyncMock:
    """FO-TD002 on the constructor, then FO-TD001 on the reassignment."""
    policy = AsyncMock(spec=OpaGatewayPolicy)
    policy.filter_tools = AsyncMock(return_value=[])
    return policy


def bad_spec_without_spec_set() -> Mock:
    """FO-TD002: `spec=` restricts names but not signatures."""
    return Mock(spec=OpaGatewayPolicy)


def bad_autospec_without_spec_set() -> MagicMock:
    """FO-TD002: create_autospec defaults to spec_set=False."""
    return create_autospec(OpaGatewayPolicy)


def bad_patch_without_autospec() -> object:
    """FO-TD003: a patch without autospec is a reassignment by another name."""
    return patch("src.mcp.policy.OpaGatewayPolicy.filter_tools")


def bad_patch_object_without_autospec() -> object:
    """FO-TD003 via `patch.object` on a project-owned target."""
    return patch.object(OpaGatewayPolicy, "authorise_call")


def bad_reasonless_suppression() -> AsyncMock:
    """FO-TD001: a bare `# noqa` is how this defect class gets waved through."""
    policy = create_autospec(OpaGatewayPolicy, spec_set=True, instance=True)
    policy.filter_tools = AsyncMock()  # noqa: FO-TD001
    return policy
