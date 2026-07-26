# `internal/validator` — structural placeholder

Required by `phases.md` 0.2, unused in Phase 0.

- **Owning future phase:** Phase 1 (agent-side validation of generated artifacts).
- **Phase 0 rule (design §1.3):** structural artifact only. This directory contains no `.go` files, no `doc.go`, no exported types, and no package behaviour. It is not a Go package.

The Phase 0 validation pipeline and Semantic Plan Analyzer live in the backend (`backend/src/analysis/plan_analyzer/`, design §11.9), not here.
