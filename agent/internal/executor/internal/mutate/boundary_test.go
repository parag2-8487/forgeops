// SPDX-License-Identifier: Apache-2.0

package mutate

import (
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// agentRoot is the module directory, three levels up from
// internal/executor/internal/mutate.
func agentRoot(t *testing.T) string {
	t.Helper()
	out, err := exec.Command("go", "list", "-m", "-f", "{{.Dir}}").Output()
	if err != nil {
		t.Fatalf("go list -m: %v", err)
	}
	return strings.TrimSpace(string(out))
}

// TestBoundary_MutateDoesNotCompileOutsideExecutor is the assertion §2.2.1 calls the
// strongest available enforcement.
//
// It is a COMPILE failure, not a lint finding and not a runtime check. A package outside
// `internal/executor/**` that imports this one cannot be built at all, so widening the
// boundary is not something a reviewer has to notice.
//
// The failure message is asserted, not just the non-zero exit. A build that failed for an
// unrelated reason — a typo in the fixture, a missing dependency — would otherwise be
// indistinguishable from the boundary holding, which is the exact shape of evidence this
// project's §0.4 regime exists to reject.
func TestBoundary_MutateDoesNotCompileOutsideExecutor(t *testing.T) {
	root := agentRoot(t)
	outsider := filepath.Join(root, "testdata", "chokepoint", "outsider")

	cmd := exec.Command("go", "build", "./testdata/chokepoint/outsider")
	cmd.Dir = root
	output, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatalf("%s compiled; the nested-internal boundary is not in force. "+
			"Any package could then write to a user's disk without a verified envelope "+
			"(design §2.2.1, D-45)", outsider)
	}
	message := string(output)
	if !strings.Contains(message, "internal") {
		t.Fatalf("the build failed, but not because of the internal rule — so this proves "+
			"nothing about the boundary. Output was:\n%s", message)
	}
	if !strings.Contains(message, "mutate") {
		t.Fatalf("the failure does not name the mutate package; the fixture may not be "+
			"importing it. Output was:\n%s", message)
	}
}

// TestBoundary_MutateCompilesInsideExecutor is the other half, and without it the test
// above is worthless.
//
// If `mutate` did not compile for any reason at all, the negative fixture would also fail
// to build and the boundary test would pass while proving nothing. This asserts the
// import IS available from inside the subtree, so the negative result is attributable to
// the boundary rather than to a broken package.
func TestBoundary_MutateCompilesInsideExecutor(t *testing.T) {
	root := agentRoot(t)
	cmd := exec.Command("go", "build", "./internal/executor/testdata/insider")
	cmd.Dir = root
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("a package INSIDE internal/executor/** cannot import mutate, so the "+
			"negative boundary test above proves nothing:\n%s", output)
	}
}

// TestBoundary_NoProductionPackageOutsideExecutorImportsMutate walks the real import graph.
//
// The two fixtures prove the RULE is in force. This proves the CURRENT tree obeys it,
// which is a different claim: a fixture cannot tell you whether some package added
// tomorrow slipped inside the subtree to reach the write path. `scripts/check-chokepoint.sh`
// runs the same query in CI over both languages; this is the in-package copy so
// `go test ./...` alone catches it.
func TestBoundary_NoProductionPackageOutsideExecutorImportsMutate(t *testing.T) {
	root := agentRoot(t)
	cmd := exec.Command("go", "list", "-deps", "-f",
		"{{.ImportPath}} {{join .Imports \" \"}}", "./...")
	cmd.Dir = root
	output, err := cmd.Output()
	if err != nil {
		t.Fatalf("go list -deps: %v", err)
	}
	const target = "github.com/parag8487/ForgeOps/agent/internal/executor/internal/mutate"
	const allowedPrefix = "github.com/parag8487/ForgeOps/agent/internal/executor"

	lines := strings.Split(strings.TrimSpace(string(output)), "\n")
	if len(lines) < 2 {
		t.Fatalf("go list returned %d lines; the check is examining nothing", len(lines))
	}
	importers := 0
	for _, line := range lines {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		importer := fields[0]
		for _, imported := range fields[1:] {
			if imported != target {
				continue
			}
			importers++
			if !strings.HasPrefix(importer, allowedPrefix) {
				t.Errorf("%s imports the mutation boundary from outside the executor subtree", importer)
			}
		}
	}
	// Vacuity guard. `./...` skips testdata, so today the only importer is the mutate
	// package's own test binary or none at all. Asserting the graph was actually read is
	// what stops this passing because `go list` returned something unexpected.
	if !strings.Contains(string(output), target) {
		t.Fatalf("the import graph does not mention %s at all; the query is wrong and the "+
			"check would pass no matter what", target)
	}
}
