# `src/auth` — structural placeholder

Required by the PRD §8 layout, deferred out of Phase 0.

- **Owning future phase:** Phase 1 (the user authentication system).
- **Phase 0 rule (design §1.3, §15.2):** structural artifact only. No `__init__.py`, no package docstring module, no importable Python module of any kind.

Phase 0 does implement OAuth 2.1/OIDC **token validation at the MCP Gateway** (`backend/src/mcp/`, design §11.4) — that is request-level verification, not a user authentication system, and it does not live here.
