# SPDX-License-Identifier: FSL-1.1-ALv2
"""Positive fixture for the Python half of check-chokepoint (design.md §2.2.1, Q-03).

The mirror of `bad_chokepoint.py`. Everything here must be reported CLEAN, and the meta test
asserts that — because a checker that flagged a legitimate governance call site would be
switched off within a week, which is pattern O's failure by another route.

The package name matters: the checker authorises a call by the **first path component** of the
module being `governance`, so this file has to sit under a `governance/` directory to exercise
that branch at all.
"""

from __future__ import annotations


def mutation_primitive(func):  # noqa: ANN001, ANN201 - a fixture stand-in for the real marker
    return func


class Writer:
    @mutation_primitive
    def append(self, payload: str) -> None:  # noqa: D102 - fixture
        pass


def inside_governance_needs_no_authority(writer: Writer) -> None:
    """CLEAN by position: §2.2.1 authorises a call lexically inside `governance/`.

    The reason it is authorised by position rather than by argument: `governance/` is the sole
    minter, so requiring it to pass itself an authority it just created would be a check that
    only ever tests that the mint ran two lines earlier.
    """
    writer.append("inside the chokepoint")
