// SPDX-License-Identifier: Apache-2.0

// No test seam may sit on a production path (design §0.4.2, §10.4, Phase 0 §5.6's honest-data
// rule).
//
// WHY A SOURCE-LEVEL GATE AND NOT A TYPE ASSERTION
// Some seams are caught by the compiler. `envelope.StaticBundleDigest` cannot be assigned to
// `session.Deps.Bundle`, because `BundleState` demands three methods it does not have — the
// compiler calls that assertion impossible. But `envelope.NewVerifier` takes a
// `BundleDigestSource`, which the seam DOES satisfy, so wiring it there would compile, pass every
// behavioural test with a digest installed by the test, and ship an agent whose Q-07 check
// compares against a value nobody set.
//
// The same shape applies to `StaticKeySource`. Its own docstring says `session` constructs one
// from the loaded Credentials — which was the plan before `CredentialKeySource` existed — so it is
// not junk, it is simply not the thing that should be reachable from `run`.
//
// Asserted by parsing this module's own non-test sources. That covers a seam introduced anywhere
// in the agent, not just in the file somebody remembered to check, and it fails at the moment the
// import lands rather than at a live pairing.
package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// productionSeams are constructors that may appear only in _test.go files.
//
// Each entry names what goes wrong if it reaches production, because a bare list invites somebody
// to add an exception rather than a replacement.
var productionSeams = map[string]string{
	"NewStaticBundleDigest": "holds a digest somebody must remember to Set; on a production " +
		"path the Q-07 bundle binding would compare against an unset value",
	"NewStaticKeySource": "holds keys in memory with no source; the real key is in the " +
		"credential Store, which is what session.CredentialKeySource reads",
}

func TestNoTestSeamOnAProductionPath(t *testing.T) {
	// Walk from the module root rather than from this package, so a seam introduced in
	// `internal/doctor` or `cmd/` is caught too.
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatalf("resolving the module root: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("expected the agent module root at %s: %v", root, err)
	}

	var scanned int
	var findings []string

	err = filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if name := entry.Name(); name == "testdata" || name == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		name := entry.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			return nil
		}
		content, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		scanned++
		text := string(content)
		relative, relErr := filepath.Rel(root, path)
		if relErr != nil {
			relative = path
		}
		for seam, why := range productionSeams {
			// The declaration itself lives in a non-test file and must not be flagged: the seams
			// are legitimate exported API for tests to use. Only a CALL is a wiring decision.
			for _, call := range []string{seam + "("} {
				index := strings.Index(text, call)
				for index >= 0 {
					if !strings.Contains(text[max(0, index-40):index], "func ") {
						findings = append(findings,
							relative+" calls "+seam+": "+why)
						break
					}
					next := strings.Index(text[index+len(call):], call)
					if next < 0 {
						break
					}
					index += len(call) + next
				}
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walking the module: %v", err)
	}

	// Vacuity guard. A walk that matched nothing because it scanned nothing would pass silently,
	// which is the failure mode of every grep-shaped test.
	if scanned < 50 {
		t.Fatalf("only %d non-test Go files scanned; the check is examining almost nothing", scanned)
	}
	for _, finding := range findings {
		t.Errorf("test seam on a production path: %s", finding)
	}
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
