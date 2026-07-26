# `internal/executor` — structural placeholder

Required by `phases.md` 0.2, unused in Phase 0.

- **Owning future phase:** Phase 1 (agent command execution, command whitelist, approval verification) and Phase 2 (Deploy, Manage & Command).
- **Phase 0 rule (design §1.3):** structural artifact only. This directory contains no `.go` files, no `doc.go`, no exported types, and no package behaviour. It is not a Go package.

A later phase replaces this marker with a real package and its tests.
