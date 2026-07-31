# `internal/executor` — the named-operation dispatcher and the mutation boundary

No longer a structural placeholder. Phase 1 leaf 7.2 filled the subtree; the dispatcher
itself arrives with leaf 8.7.

## What is here now

```
internal/executor/
├── internal/
│   └── mutate/          # the agent's ONLY file-writing code (design §10.5, D-45)
└── testdata/
    └── insider/         # boundary fixture: a package INSIDE the subtree may import mutate
```

## Why `internal/mutate` is nested

Go's nested-`internal` rule means a package under `agent/internal/executor/internal/…` is
importable **only** by packages rooted at `agent/internal/executor/`. That is a
compile-time boundary, not a convention: a package elsewhere in the module that imports
`mutate` **does not build**. Design §2.2.1 calls this the strongest available enforcement
because it needs no lint, no review step and no discipline.

Two fixtures prove the rule is in force rather than merely intended, and both are needed:

- `agent/testdata/chokepoint/outsider` sits outside the subtree and **must not compile**;
- `internal/executor/testdata/insider` sits inside it and **must compile**.

Without the second, the first would also pass if `mutate` were simply broken.
`internal/executor/internal/mutate/boundary_test.go` builds each by explicit path and
asserts the failure message names the internal rule, so a build that broke for an
unrelated reason cannot be read as the boundary holding. `scripts/check-chokepoint.sh`
makes the same assertion over the whole import graph in CI.

Both fixture directories are named `testdata`, so `go build ./...` and `go test ./...`
never build them as part of the module.

## What still arrives later

- `executor.go` — the closed named-operation dispatch table taking a `*envelope.Verified`
  (design §10.5, leaf 8.7).
- `contract_test.go` — `var _ Dispatcher = (*dispatcher)(nil)`.

Design references: §2.2.1, §10.1, §10.5, §17.1 D-45, §17.1 D-59.
