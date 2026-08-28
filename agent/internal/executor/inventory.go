// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/fileops"
	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/secretscan"
)

// `readiness.inventory` and `secretscan.run` — the two remaining catalogued reads.
//
// Both are read-only, so neither is `mutating` and neither requires an approval, for the same reason
// the validators do not: a user should not have to approve being told what is in their own workspace.

// inventoryArgs asks for an inventory of the workspace.
//
// No path: the inventory is of the workspace the agent was configured with, whole. A path argument
// would let a caller narrow it, and a readiness score computed over a subdirectory while claiming to
// describe the project is exactly the kind of number this project keeps removing.
type inventoryArgs struct {
	ProjectID string `json:"project_id"`
}

// readinessInventory reports the deployment-readiness inventory of the workspace (FR-11, FR-16).
//
// WHAT THIS ADDS OVER `scan.full`. A scan builds the index — the file tree, the chunks, the vectors —
// and is measured in minutes over a large repository. Readiness scoring needs a much smaller thing:
// which manifests, config files and entry points exist, which languages are present, and how big the
// tree is. Answering that by running a full scan would make a readiness refresh cost an index
// rebuild, which is why §7.7 catalogues it separately.
//
// The counts and paths travel; no file content does. `Inventory` carries relative paths only, so the
// result cannot leak the contents of a config file it names.
func readinessInventory(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args inventoryArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("executor: undecodable inventory arguments: %w", err)
	}
	if strings.TrimSpace(args.ProjectID) == "" {
		return Result{}, errors.New("executor: an inventory needs a project_id to report against")
	}

	sink.Progress(10, "readiness.inventory", "walking the workspace")
	filtered := scanner.NewFilteredScanner(0, "")
	inventory, err := filtered.ScanDirectory(d.root)
	if err != nil {
		return Result{}, fmt.Errorf("executor: inventory failed: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return Result{}, fmt.Errorf("executor: inventory did not finish: %w", err)
	}
	sink.Progress(80, "readiness.inventory", fmt.Sprintf(
		"%d file(s), %d manifest(s), %d config file(s), %d entry point(s)",
		inventory.FileCount, len(inventory.Manifests), len(inventory.ConfigFiles), len(inventory.EntryPoints)))

	encoded, err := json.Marshal(inventory)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable inventory: %w", err)
	}
	sink.Progress(100, "readiness.inventory", "inventory complete")
	return Result{Status: "ok", Output: string(encoded)}, nil
}

// secretScanArgs asks for a secret scan of a path in the workspace.
type secretScanArgs struct {
	ProjectID string `json:"project_id"`
	// Path narrows the scan. Empty means the whole workspace.
	Path string `json:"path,omitempty"`
}

// SecretScanReport is what `secretscan.run` reports (FR-42).
//
// FINDINGS WITHOUT VALUES, AND THAT IS THE DESIGN. Every finding carries its kind, path, line and
// gitleaks fingerprint, and never the matched text. A secret scanner's report travels to the backend
// and into an audit trail that is deliberately append-only and hash-chained, so putting the
// credential in it would make the tamper-evident log the most durable copy of the leak.
//
// The fingerprint is what makes a finding actionable without the value: it is stable across runs, so
// an operator can tell a new leak from one they have already triaged.
type SecretScanReport struct {
	FilesScanned int `json:"files_scanned"`
	// FindingCount is separate from `len(Findings)` because a scan can be truncated; when it is,
	// the count is the truth and the list is a sample.
	FindingCount int                 `json:"finding_count"`
	Findings     []SecretScanFinding `json:"findings"`
	Truncated    bool                `json:"truncated"`
	// ScannerVersion names what did the scanning, for the same reason a validator does.
	ScannerVersion string `json:"scanner_version"`
}

// SecretScanFinding is one detection, with the value deliberately withheld.
type SecretScanFinding struct {
	Kind        string  `json:"kind"`
	Path        string  `json:"path"`
	Line        int     `json:"line"`
	Fingerprint string  `json:"fingerprint"`
	Entropy     float32 `json:"entropy"`
}

// : A bound on how many findings travel. A repository that is entirely credentials should not be able
// : to produce a `command.result` frame §7.3 cannot carry.
const maxSecretFindings = 500

// secretScanRun scans the workspace for credentials with the same gitleaks engine the indexer uses.
//
// THE SAME SCANNER THE INDEX USES, DELIBERATELY. `secretscan.NewScanner` is what redacts chunks
// before they leave the machine, so running it standalone here means the answer to "what would be
// redacted" and the answer to "what did you find" cannot disagree. A separate rule set for reporting
// would eventually drift from the one that protects the data.
func secretScanRun(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args secretScanArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("executor: undecodable secret scan arguments: %w", err)
	}
	if strings.TrimSpace(args.ProjectID) == "" {
		return Result{}, errors.New("executor: a secret scan needs a project_id to report against")
	}

	root := d.root
	if rel := strings.TrimSpace(args.Path); rel != "" {
		resolved, err := fileops.ResolveForRead(d.root, rel)
		if err != nil {
			return Result{}, fmt.Errorf("executor: %w", err)
		}
		root = resolved
	}

	sink.Progress(5, "secretscan.run", "starting gitleaks")
	engine, err := secretscan.NewScanner()
	if err != nil {
		return Result{}, fmt.Errorf("executor: secret scanner unavailable: %w", err)
	}

	report := SecretScanReport{ScannerVersion: secretscan.EngineVersion()}
	walker := scanner.NewFilteredScanner(0, "")
	err = walker.WalkFiles(ctx, root, func(relPath string, content []byte) error {
		report.FilesScanned++
		if report.FilesScanned%200 == 0 {
			sink.Progress(50, "secretscan.run", fmt.Sprintf("%d file(s) scanned", report.FilesScanned))
		}
		findings, scanErr := engine.Scan(ctx, relPath, content)
		if scanErr != nil {
			return fmt.Errorf("scanning %s: %w", relPath, scanErr)
		}
		for _, f := range findings {
			report.FindingCount++
			if len(report.Findings) >= maxSecretFindings {
				report.Truncated = true
				continue
			}
			report.Findings = append(report.Findings, SecretScanFinding{
				Kind:        f.Kind,
				Path:        filepath.ToSlash(relPath),
				Line:        f.Line,
				Fingerprint: f.Fingerprint,
				Entropy:     f.Entropy,
			})
		}
		return nil
	})
	if err != nil {
		return Result{}, fmt.Errorf("executor: secret scan failed: %w", err)
	}

	// Deterministic order, so two scans of an unchanged tree produce the same report and a diff
	// between them means something changed rather than that a map iterated differently.
	sort.Slice(report.Findings, func(i, j int) bool {
		if report.Findings[i].Path != report.Findings[j].Path {
			return report.Findings[i].Path < report.Findings[j].Path
		}
		return report.Findings[i].Line < report.Findings[j].Line
	})

	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable secret scan report: %w", err)
	}
	// `clean` versus `findings` rather than `ok` for both: FR-42 exists so a caller can act on the
	// difference, and a status it has to parse a payload to discover is not much of a status.
	status := "clean"
	if report.FindingCount > 0 {
		status = "findings"
	}
	sink.Progress(100, "secretscan.run", fmt.Sprintf(
		"%d file(s) scanned, %d finding(s)", report.FilesScanned, report.FindingCount))
	return Result{Status: status, Output: string(encoded)}, nil
}

// ensure the imports are all used even on platforms where os is only needed transitively.
var _ = os.Stat
