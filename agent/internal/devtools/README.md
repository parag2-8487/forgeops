# `internal/devtools` — structural placeholder

Required by `phases.md` 0.2, unused in Phase 0.

- **Owning future phase:** a later phase that adds agent-side developer-tooling integrations.
- **Phase 0 rule (design §1.3):** structural artifact only. This directory contains no `.go` files, no `doc.go`, no exported types, and no package behaviour. It is not a Go package.

The Phase 0 `devtools` Docker build target (OpenTofu 1.12.5, design §13.3) belongs to `agent/Dockerfile`, not to this directory.
