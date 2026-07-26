# `internal/policy` — structural placeholder

Required by `phases.md` 0.2, unused in Phase 0.

- **Owning future phase:** Phase 1 (OPA-Wasm policy evaluation inside the agent, Governance Control Plane).
- **Phase 0 rule (design §1.3):** structural artifact only. This directory contains no `.go` files, no `doc.go`, no exported types, and no package behaviour. It is not a Go package.

Phase 0 enforces policy only at the backend MCP Gateway against the OPA server (`policies/`, design §5.4, §11.4).
