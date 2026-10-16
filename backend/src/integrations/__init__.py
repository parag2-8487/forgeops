# SPDX-License-Identifier: FSL-1.1-ALv2
"""Per-user integrations with outside services.

Currently one: the GitHub account link. It is a module of its own rather than a corner of `projects/`
because a link belongs to a PERSON and outlives any project they create with it, and rather than a
corner of `auth/` because it is not authentication — Authentik OIDC remains the only way to sign in and
nothing here produces a `Principal`.
"""
