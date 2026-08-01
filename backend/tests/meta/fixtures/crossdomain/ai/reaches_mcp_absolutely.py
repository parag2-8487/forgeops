# SPDX-License-Identifier: FSL-1.1-ALv2
"""NEGATIVE CONTROL: the same crossing written as an ABSOLUTE import (design.md §2.2.1).

The tree uses relative imports on purpose — `TID252` is disabled so `core.errors.ProblemException`
is one class object in every importer — so a checker built only for relative imports would look
correct on this repository and be trivially bypassable by writing the crossing the other way. This
file must be REPORTED for the same reason `reaches_mcp.py` is.
"""

from __future__ import annotations

from src.mcp.gateway import Gateway  # noqa: F401  BANNED, and written absolutely


def reaches_across_absolutely() -> None:
    """Body irrelevant; the import above is the whole point of the fixture."""
