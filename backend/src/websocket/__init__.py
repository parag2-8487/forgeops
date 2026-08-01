# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent WebSocket hub (design.md §7.3, §11.10).

`hub.py` is the session; `routes.py` is the one route that reaches it. The handshake's
authentication lives in the route rather than in the hub, because it needs the device service —
and `src.auth.devices` is banned outside `governance/` (§2.4), so the hub takes an
already-authenticated peer through a Protocol it declares itself.
"""
