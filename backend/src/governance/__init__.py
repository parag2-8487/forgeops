# SPDX-License-Identifier: FSL-1.1-ALv2
"""`governance` — the §1.10 chokepoint and the change-set lifecycle it guards.

This package exists although PRD §8 has no module for §1.10, the same situation
`backend/src/mcp/` was in during Phase 0 and resolved the same way (design §2.4).

The change-set tables live here rather than in a `changes/` package because the
chokepoint owns their lifecycle: a change set is only ever created, approved and
applied through §11.6, and putting its schema next to the code that mints the
authority keeps the boundary legible.
"""
