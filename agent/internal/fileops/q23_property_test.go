// SPDX-License-Identifier: Apache-2.0
package fileops

import (
	"reflect"
	"strings"
	"testing"

	"pgregory.net/rapid"
)

// Property Q-23 (design Appendix B; tasks.md leaf 16.4).
//
//	∀ change items: the unified diff rendered for approval captures every edit losslessly.
//
// # WHAT THIS REPLACES
//
// Q-23's property file was `backend/tests/property/test_q23_diff_fidelity.py`, and it imported
// `difflib` and `hypothesis` and nothing else. It called `difflib.unified_diff` — Python's
// standard library — and asserted that identical inputs produce an empty diff and different
// inputs produce a non-empty one. It never touched `FileOps.UnifiedDiff`, which is the differ
// whose output a human actually approves, so it was a test of CPython rather than of ForgeOps.
// It could not be given a negative control for the same reason Q-13 could not: no mutation of
// the agent's differ is observable from a test that never calls it.
//
// Appendix B names `fileops.UnifiedDiff` as a target and it lives here, in Go, so the property
// moved to the code rather than the code being re-implemented in the test.
//
// # THE INVARIANT
//
// A unified diff is lossless exactly when it is a complete edit script: reading the context and
// deletion lines reproduces the BEFORE text, and reading the context and insertion lines
// reproduces the AFTER text. That single property subsumes "every addition appears with a `+`"
// and "no line is silently dropped", and it fails under any mutation that loses a line, mislabels
// one, or emits them out of order — which is why it is stated this way rather than as a pair of
// set-membership checks that a reordering mutation would survive.
//
// Empty lines are excluded on both sides because `UnifiedDiff` skips them by construction
// (`if line == "" { continue }`), so including them would assert a behaviour the differ does not
// claim.
func TestPropertyQ23_UnifiedDiffCapturesEveryEditLosslessly(t *testing.T) {
	ops := New()

	rapid.Check(t, func(rt *rapid.T) {
		lineGen := rapid.StringMatching(`[a-zA-Z0-9 ]{1,12}`)
		beforeLines := rapid.SliceOfN(lineGen, 0, 8).Draw(rt, "beforeLines")
		afterLines := rapid.SliceOfN(lineGen, 0, 8).Draw(rt, "afterLines")

		before := joinLines(beforeLines)
		after := joinLines(afterLines)

		diff := ops.UnifiedDiff(before, after, "subject.txt")

		// The header is part of the contract: without it no reviewer can tell what changed.
		wantHeader := "--- a/subject.txt\n+++ b/subject.txt\n"
		if !strings.HasPrefix(diff, wantHeader) {
			rt.Fatalf("Q-23 violation: diff does not start with its file header, got %q", diff)
		}

		body := strings.TrimPrefix(diff, wantHeader)

		var rebuiltBefore, rebuiltAfter []string
		for _, line := range strings.Split(body, "\n") {
			if line == "" {
				continue
			}
			marker, content := line[0], line[1:]
			switch marker {
			case ' ':
				rebuiltBefore = append(rebuiltBefore, content)
				rebuiltAfter = append(rebuiltAfter, content)
			case '-':
				rebuiltBefore = append(rebuiltBefore, content)
			case '+':
				rebuiltAfter = append(rebuiltAfter, content)
			default:
				rt.Fatalf("Q-23 violation: diff line carries an unknown marker %q: %q", marker, line)
			}
		}

		expectBefore := nonEmpty(beforeLines)
		expectAfter := nonEmpty(afterLines)

		if !reflect.DeepEqual(rebuiltBefore, expectBefore) {
			rt.Fatalf(
				"Q-23 violation: context+deletions do not reproduce the BEFORE text.\n"+
					"  rebuilt: %q\n  expected: %q\n  diff was:\n%s",
				rebuiltBefore, expectBefore, diff,
			)
		}
		if !reflect.DeepEqual(rebuiltAfter, expectAfter) {
			rt.Fatalf(
				"Q-23 violation: context+insertions do not reproduce the AFTER text.\n"+
					"  rebuilt: %q\n  expected: %q\n  diff was:\n%s",
				rebuiltAfter, expectAfter, diff,
			)
		}
	})
}

// TestPropertyQ23_IdenticalInputsProduceNoEditLines is the boundary case the property above
// cannot distinguish: reconstruction succeeds for an all-context diff and for a diff that
// deletes then re-inserts every line, and only one of those is correct to show a reviewer.
func TestPropertyQ23_IdenticalInputsProduceNoEditLines(t *testing.T) {
	ops := New()

	rapid.Check(t, func(rt *rapid.T) {
		lines := rapid.SliceOfN(rapid.StringMatching(`[a-zA-Z0-9 ]{1,12}`), 1, 8).Draw(rt, "lines")
		text := joinLines(lines)

		diff := ops.UnifiedDiff(text, text, "subject.txt")

		for _, line := range strings.Split(diff, "\n") {
			if strings.HasPrefix(line, "---") || strings.HasPrefix(line, "+++") || line == "" {
				continue
			}
			if line[0] == '+' || line[0] == '-' {
				rt.Fatalf(
					"Q-23 violation: identical before/after produced an edit line %q. A reviewer "+
						"would be asked to approve a change that does not exist",
					line,
				)
			}
		}
	})
}

func joinLines(lines []string) string {
	if len(lines) == 0 {
		return ""
	}
	return strings.Join(lines, "\n") + "\n"
}

func nonEmpty(lines []string) []string {
	var out []string
	for _, line := range lines {
		if line != "" {
			out = append(out, line)
		}
	}
	return out
}
