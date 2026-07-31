// SPDX-License-Identifier: Apache-2.0

// Package envelope's interface obligations and its leaf-package invariant, stated in one
// greppable place (design §0.4.2, §17.1 D-59).
package envelope

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"
)

var (
	_ ReplayGuard        = (*MemoryReplayGuard)(nil)
	_ KeySource          = (*StaticKeySource)(nil)
	_ BundleDigestSource = (*StaticBundleDigest)(nil)
)

// TestPackageIsALeaf is the assertion D-59 rests on.
//
// The whole reason this package exists rather than living in `session` is that `session`
// imports `executor`, so a `Verified` in `session` would close the cycle
// session -> executor -> executor/internal/mutate -> session. That argument holds only
// while `envelope` imports nothing from `internal/**`: one convenience import of
// `internal/session` or `internal/config` would reintroduce the cycle from the other
// direction, and the failure would be a build error in a *different* package, which is a
// confusing place to learn about it.
//
// Asserted against `go list` rather than by grepping the source, so a transitive import
// through a helper file is caught too.
func TestPackageIsALeaf(t *testing.T) {
	out, err := exec.Command("go", "list", "-json", ".").Output()
	if err != nil {
		t.Fatalf("go list: %v", err)
	}
	var pkg struct {
		ImportPath  string
		Imports     []string
		TestImports []string
	}
	if err := json.Unmarshal(out, &pkg); err != nil {
		t.Fatalf("unmarshal go list output: %v", err)
	}
	if pkg.ImportPath == "" {
		t.Fatal("go list returned no ImportPath; the assertion would be vacuous")
	}
	// Vacuity guard: a package with no imports at all would pass trivially, and this
	// package genuinely imports several standard-library packages.
	if len(pkg.Imports) == 0 {
		t.Fatal("go list reported no imports; the check is examining nothing")
	}
	const forbidden = "github.com/parag8487/ForgeOps/agent/internal/"
	for _, imported := range pkg.Imports {
		if strings.HasPrefix(imported, forbidden) {
			t.Fatalf("internal/envelope must be a LEAF package and imports %s; that reopens "+
				"D-59's cycle (session -> executor -> mutate -> envelope -> ...)", imported)
		}
	}
}
