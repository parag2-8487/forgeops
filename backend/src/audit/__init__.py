# SPDX-License-Identifier: FSL-1.1-ALv2
"""`audit` — the append-only, hash-chained event log (design.md §1.9, §11.9).

This package exists although PRD §8 has no module for §1.9. Folding it into
`core/` would make the append-only boundary a convention rather than a package,
and the whole value of the boundary is that it is not a convention.
"""
