// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

// TestBuildReport_NoSliceIsSerialisedAsNull is a WIRE-CONTRACT test, not a style one.
//
// Go marshals a nil slice as `null`. The backend's `ScanReportIn` declares `chunks` and
// `dependencies` as required lists, and pydantic refuses `null` for a list — so one nil slice
// anywhere in the tree made the backend reject the WHOLE report with a 422, losing the index for
// every other file in it. A real scan of `backend/src` did exactly that: seven zero-byte
// `.gitkeep` files produced `"chunks": null`, and 141 files' worth of index was refused because
// of them.
//
// Asserted on the SERIALISED FORM rather than on the Go values, because that is where the
// mismatch lives: `len(chunks) == 0` is true for both nil and empty, and only one of them
// survives the round trip.
func TestBuildReport_NoSliceIsSerialisedAsNull(t *testing.T) {
	root := t.TempDir()
	// A zero-byte file and a whitespace-only one: both take the early return in `chunk`, which
	// is where the nil came from.
	write := func(name, body string) {
		t.Helper()
		full := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("MkdirAll: %v", err)
		}
		if err := os.WriteFile(full, []byte(body), 0o600); err != nil {
			t.Fatalf("WriteFile: %v", err)
		}
	}
	write(".gitkeep", "")
	write("pkg/.gitkeep", "")
	write("blank.py", "\n")
	// And one real file, so the report is not vacuously empty.
	write("main.py", "import os\n\n\ndef main():\n    return os.getcwd()\n")

	redactor, err := secretscan.NewScanner()
	if err != nil {
		t.Fatalf("NewScanner: %v", err)
	}
	rs, err := NewReportScanner(DefaultMaxFileSize, "", redactor)
	if err != nil {
		t.Fatalf("NewReportScanner: %v", err)
	}
	report, err := rs.BuildReport(context.Background(), root)
	if err != nil {
		t.Fatalf("BuildReport: %v", err)
	}
	if len(report.Files) == 0 {
		t.Fatal("no files were scanned, so this test proves nothing")
	}

	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	for _, forbidden := range []string{
		`"chunks":null`,
		`"dependencies":null`,
		`"files":null`,
		`"languages":null`,
		`"manifests":null`,
		`"config_files":null`,
		`"entry_points":null`,
	} {
		if strings.Contains(string(encoded), forbidden) {
			t.Errorf("the report serialises %s, which the backend refuses as a non-list", forbidden)
		}
	}

	// The empty case must still be REACHED, or the assertion above is vacuous: a scanner that
	// stopped producing chunkless files would pass without checking anything.
	sawEmpty := false
	for _, f := range report.Files {
		if len(f.Chunks) == 0 {
			sawEmpty = true
			break
		}
	}
	if !sawEmpty {
		t.Fatal("no file produced zero chunks, so the null-vs-empty distinction was never exercised")
	}
}
