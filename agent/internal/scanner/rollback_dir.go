package scanner

// rollbackStateDirName is the directory an apply writes its pre-images and single-use consumption
// markers into, at the workspace root.
//
// Duplicated as a literal rather than imported from `internal/executor/internal/mutate`: that package is
// under an `internal/` boundary the scanner is not permitted to cross, and a scanner that imported the
// executor would invert the dependency. The two are pinned together by
// TestTheScannerSkipsTheSameDirectoryTheExecutorWritesTo, which fails if either side changes alone.
const rollbackStateDirName = ".forgeops-rollback"
