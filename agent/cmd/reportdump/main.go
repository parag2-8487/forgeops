// SPDX-License-Identifier: Apache-2.0

// Command reportdump prints a real scan report as JSON, for the cross-language wire-contract check.
//
// WHY THIS EXISTS. `TestBuildReport_NoSliceIsSerialisedAsNull` asserts the Go side never emits `null`
// where the backend declares a list, and it caught FR-10's two new fields. What it cannot check is the
// other direction: whether the values the Go side DOES emit satisfy the backend's own validators —
// `ScanFrameworkIn.kind` is a `Literal`, `evidence` has `min_length=1`, and a mismatch in either is a 422
// that rejects the whole report.
//
// So this dumps a report built from a real tree and `backend/tests/integration/test_scan_report_contract.py`
// feeds it to the real pydantic model. Two languages, one contract, checked by running both halves rather
// than by reading them.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

func main() {
	root := flag.String("root", ".", "directory to scan")
	flag.Parse()

	redactor, err := secretscan.NewScanner()
	if err != nil {
		fmt.Fprintf(os.Stderr, "reportdump: secret scanner: %v\n", err)
		os.Exit(1)
	}
	rs, err := scanner.NewReportScanner(scanner.DefaultMaxFileSize, "", redactor)
	if err != nil {
		fmt.Fprintf(os.Stderr, "reportdump: report scanner: %v\n", err)
		os.Exit(1)
	}
	report, err := rs.BuildReport(context.Background(), *root)
	if err != nil {
		fmt.Fprintf(os.Stderr, "reportdump: build: %v\n", err)
		os.Exit(1)
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		fmt.Fprintf(os.Stderr, "reportdump: marshal: %v\n", err)
		os.Exit(1)
	}
	if _, err := os.Stdout.Write(encoded); err != nil {
		fmt.Fprintf(os.Stderr, "reportdump: write: %v\n", err)
		os.Exit(1)
	}
}
