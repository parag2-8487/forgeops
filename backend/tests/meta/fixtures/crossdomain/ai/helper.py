# SPDX-License-Identifier: FSL-1.1-ALv2
"""A module in `ai/`'s own namespace, so `stays_within_domain.py` has something local to import."""

from __future__ import annotations


def within_domain() -> str:
    return "ai-local"
