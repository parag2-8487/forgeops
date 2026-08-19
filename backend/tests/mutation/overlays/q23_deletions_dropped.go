// SPDX-License-Identifier: Apache-2.0

// Package fileops holds the agent's path validation and diff rendering.
//
// It does NOT write files. It used to: Phase 0 exported `Ops.ApplyAtomic`, and D-45
// supersedes it with `executor/internal/mutate.ApplyVerified` for one reason —
// "an exported write function that any package can call is a bypass waiting to be
// written". The write implementation moved into a nested-`internal` package importable
// only from within `internal/executor/**`, so a mutation without a verified envelope is
// a compile error rather than a review miss (§2.2.1, §10.5, D-45, D-59).
//
// What stays here, and why: the path rules are needed on BOTH intents. Reading a file
// into a prompt is judged by `ResolveForRead`, writing one by `ResolveForWrite`, and
// D-46 makes those two rules deliberately different. Keeping them in one package is
// what makes "the write list is the read list plus exactly three names" a statement a
// test can check, rather than two implementations that have to be compared by eye.
//
// `UnifiedDiff` stays because it renders a diff for a human to approve; it writes
// nothing.
package fileops

// NEGATIVE CONTROL for Q-23. Applied by `scripts/mutation-harness.py` via `go build -overlay`,
// which substitutes this file for `agent/internal/fileops/fileops.go` for the duration of one test
// run. It is never compiled into the agent.
//
// Byte-for-byte the committed `fileops.go` except for one branch in `UnifiedDiff`: deletion lines
// are no longer emitted. Appendix B's prescribed control is "compile change items with
// `old_content` from the wrong revision", which yields a diff whose BEFORE side does not match
// reality. Dropping the deletions is that same defect expressed inside the differ, and it is the
// form this property can observe.
//
// GENERATED from the committed original, so it cannot drift from it. Regenerate after any change to
// `UnifiedDiff` -- it was regenerated when `DiffCleanupSemantic` was removed from the line-mode
// path.
//
// With this applied, TestPropertyQ23_UnifiedDiffCapturesEveryEditLosslessly must FAIL: context plus
// deletions stop reconstructing the before text as soon as any line is removed.

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/sergi/go-diff/diffmatchpatch"
)

// Errors
var (
	ErrPathOutsideRoot = errors.New("path resolves outside root")
	ErrPathBlocked     = errors.New("path matches blocklist")
)

// Ops is the file operations interface.
//
// One method. It carried `ApplyAtomic` in Phase 0; D-45 removed it, and the removal is
// the point — a consumer holding an `Ops` can no longer write anything, so `app` and
// `mcp` can keep their dependency without holding a write capability they must not have
// (D-47: the agent's MCP server gains only non-mutating tools).
type Ops interface {
	UnifiedDiff(before, after, label string) string
}

// FileOps implements Ops.
type FileOps struct{}

// New creates a new FileOps instance.
func New() *FileOps {
	return &FileOps{}
}

// UnifiedDiff produces a unified diff string using sergi/go-diff.
//
// Unchanged from Phase 0. Q-23 asserts that applying this diff to `before` yields
// `after` and that the frontend renders the same hunk count the backend computed.
func (f *FileOps) UnifiedDiff(before, after, label string) string {
	dmp := diffmatchpatch.New()
	a, b, c := dmp.DiffLinesToChars(before, after)
	diffs := dmp.DiffMain(a, b, false)
	diffs = dmp.DiffCharsToLines(diffs, c)

	// NO `DiffCleanupSemantic` here, and that is a fix rather than an omission.
	//
	// `DiffCleanupSemantic` is a CHARACTER-mode cleanup. Applied after `DiffCharsToLines` it
	// re-merges edits across line boundaries, so the rendered diff stops corresponding to the
	// change. Q-23 found it immediately: for `before = "0"` and `after = " 0"` — one line gaining a
	// leading space — the cleanup produced
	//
	//	+ ⟨space⟩
	//	 0
	//
	// an INSERTED blank-ish line plus "0" unchanged, where the file actually has a single line that
	// changed. Reading the context and insertion lines back yields ["⟨space⟩", "0"] instead of
	// [" 0"], so the diff is not a faithful edit script — and a reviewer approving it is approving a
	// misrepresentation of the change, which is the one thing this function exists to prevent.
	//
	// The line-mode recipe is LinesToChars → Main → CharsToLines, with no semantic pass. Cleanup
	// belongs to character diffs, where merging adjacent edits genuinely reads better.

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("--- a/%s\n+++ b/%s\n", label, label))
	for _, d := range diffs {
		lines := strings.Split(d.Text, "\n")
		for _, line := range lines {
			if line == "" {
				continue
			}
			switch d.Type {
			case diffmatchpatch.DiffDelete:
				// THE MUTATION (Q-23 negative control): the deletion line is dropped, so the
				// rendered diff no longer reproduces the BEFORE text. A reviewer approving this
				// diff cannot see what is being removed.
				_ = line
			case diffmatchpatch.DiffInsert:
				sb.WriteString("+" + line + "\n")
			case diffmatchpatch.DiffEqual:
				sb.WriteString(" " + line + "\n")
			}
		}
	}
	return sb.String()
}

// ResolveForRead resolves relPath under root for READ intent.
//
// Phase 0's `resolveAndValidate`, exported and unchanged in behaviour. P-08's read clause
// asserts exactly this strictness: `.env`, `.env.*` in any case, `*.pem`, `~/.ssh` and
// `~/.aws` are all refused, with no exemptions of any kind. There is deliberately no
// exemption list on this path — reading a real `.env` into an LLM prompt is the harm
// D-46's read rule exists to prevent.
func ResolveForRead(root, relPath string) (string, error) {
	return resolve(root, relPath, blockedForRead)
}

// ResolveForWrite resolves relPath under root for WRITE intent.
//
// Identical to ResolveForRead except that D-46's three exact names — `.env.example`,
// `.env.sample`, `.env.template` — are permitted, because §1.5 lists a generated
// `.env.example` as one of the platform's own artifacts. Everything else, including a
// case variant or a `.bak` of an exemption, stays refused.
//
// This is the function the mutation boundary calls. Until D-45's move it had NO caller:
// leaf 4.7 split the blocklist by intent and Phase 0's `ApplyAtomic` still went through
// the read path, so the write rule was correct and unreachable. Wiring it is part of
// what leaf 7.2 is for.
func ResolveForWrite(root, relPath string) (string, error) {
	return resolve(root, relPath, blockedForWrite)
}

// resolve is Phase 0's `resolveAndValidate`, parameterised by the blocklist to consult.
//
// The containment logic is byte-for-byte the Phase 0 logic. Only the final blocklist call
// is a parameter, so the two intents cannot drift in how they resolve symlinks or check
// root containment — they differ in exactly one decision and are otherwise the same code
// path.
func resolve(root, relPath string, blocked func(string) bool) (string, error) {
	// Clean the path
	cleaned := filepath.Clean(relPath)
	if filepath.IsAbs(cleaned) {
		return "", fmt.Errorf("%w: %s is absolute", ErrPathOutsideRoot, relPath)
	}

	joined := filepath.Join(root, cleaned)

	// Resolve symlinks for containment check
	resolved, err := filepath.EvalSymlinks(filepath.Dir(joined))
	if err != nil {
		// If parent doesn't exist yet, resolve what we can
		resolved, err = filepath.EvalSymlinks(root)
		if err != nil {
			return "", fmt.Errorf("resolve root: %w", err)
		}
		joined = filepath.Join(resolved, cleaned)
	} else {
		joined = filepath.Join(resolved, filepath.Base(joined))
	}

	// Root containment check
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", fmt.Errorf("resolve root: %w", err)
	}

	// Normalize for comparison
	resolvedRootNorm := filepath.Clean(resolvedRoot) + string(filepath.Separator)
	joinedNorm := filepath.Clean(joined)

	if !strings.HasPrefix(joinedNorm+string(filepath.Separator), resolvedRootNorm) &&
		joinedNorm != filepath.Clean(resolvedRoot) {
		return "", fmt.Errorf("%w: %s escapes root %s", ErrPathOutsideRoot, relPath, root)
	}

	// Blocklist check
	if blocked(joined) {
		return "", fmt.Errorf("%w: %s", ErrPathBlocked, relPath)
	}

	return joined, nil
}

// BlockedForRead reports whether reading absPath is refused.
//
// Exported so `mutate` and the secret scanner can consult the rule without each
// reimplementing it, and so a test can assert the two intents differ on exactly three
// names. The absolute path is the argument because the `~/.ssh` and `~/.aws` clauses are
// about location, not about filename.
func BlockedForRead(absPath string) bool { return blockedForRead(absPath) }

// BlockedForWrite reports whether writing absPath is refused (D-46).
func BlockedForWrite(absPath string) bool { return blockedForWrite(absPath) }
