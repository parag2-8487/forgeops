# SPDX-License-Identifier: FSL-1.1-ALv2
"""`core` stands in for the one dependency every domain is told to have."""

from __future__ import annotations


class ProblemException(Exception):  # noqa: N818  mirrors the production name deliberately
    """Stands in for `src.core.errors.ProblemException`. Never raised."""
