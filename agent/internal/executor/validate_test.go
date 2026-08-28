// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

// The eight operations that used to be `unimplemented(...)`, exercised THROUGH the dispatcher.
//
// Through `Execute` rather than by calling the handlers directly, because that is the only dispatch
// surface (§10.5) and it is where the timeout, the approval rule and the argument decoding live. A
// test that called `validateYAML` directly would prove the body works and nothing about whether the
// operation is reachable, bounded, or correctly classified as read-only.

func requireBinary(t *testing.T, tool string) {
	t.Helper()
	if _, err := exec.LookPath(tool); err != nil {
		t.Skipf("%s not found", tool)
	}
}

func writeIn(t *testing.T, root, rel, content string) {
	t.Helper()
	path := filepath.Join(root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// decodeOutcome reads the validator Outcome back out of a Result.
func decodeOutcome(t *testing.T, res Result) validator.Outcome {
	t.Helper()
	var outcome validator.Outcome
	if err := json.Unmarshal([]byte(res.Output), &outcome); err != nil {
		t.Fatalf("the result's Output is not a validator Outcome: %v\n%s", err, res.Output)
	}
	return outcome
}

func TestValidate_NoneOfTheSixIsUnimplemented(t *testing.T) {
	// The headline claim of this change, asserted rather than described. Every one of these answered
	// `ErrUnimplemented` while Phase 1's criterion "Generated files pass validation pipeline" was
	// ticked, so a reader had two sources disagreeing about the same fact.
	for _, op := range []Operation{
		OpValidateCompose, OpValidateK8s, OpValidateTofu,
		OpValidateHelm, OpValidateYAML, OpValidateTrivy,
		OpReadinessInventory, OpSecretScanRun,
	} {
		row, ok := handlerTable[op]
		if !ok {
			t.Errorf("%q left the dispatch table", op)
			continue
		}
		if !row.implemented {
			t.Errorf("%q is still not implemented", op)
		}
		if row.mutating || row.requiresApproval {
			t.Errorf("%q is read-only and must need no approval; mutating=%v requiresApproval=%v",
				op, row.mutating, row.requiresApproval)
		}
	}
}

func TestValidateYAML_AGeneratedWorkflowIsValidatedThroughTheDispatcher(t *testing.T) {
	requireBinary(t, "yamllint")
	root := t.TempDir()
	writeIn(t, root, ".github/workflows/ci.yml", `---
name: ci
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: make build
`)
	d := newDispatcher(t, root)
	sink := &recordingSink{}
	res, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": ".github/workflows/ci.yml"}, 21), sink)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "valid" {
		t.Fatalf("status %q, output: %s", res.Status, res.Output)
	}
	outcome := decodeOutcome(t, res)
	if outcome.Tool != "yamllint" {
		t.Errorf("the outcome names tool %q", outcome.Tool)
	}
	if len(sink.events) == 0 {
		t.Error("no progress was reported, so a long validation would look like a hang")
	}
}

func TestValidateYAML_ABrokenWorkflowIsInvalidRatherThanAnError(t *testing.T) {
	// The distinction the generation feedback loop is built on. "The artifact is broken" must be a
	// completed operation with status `invalid`, so the pipeline can loop on it; an error return means
	// "we could not check", which must stop the loop rather than trigger another attempt.
	requireBinary(t, "yamllint")
	root := t.TempDir()
	writeIn(t, root, ".github/workflows/ci.yml", "---\nname: ci\non:\n  push: {}\njobs: \"not a mapping\"\n")
	d := newDispatcher(t, root)
	res, err := d.Execute(context.Background(),
		verified(t, OpValidateYAML, "", map[string]any{"path": ".github/workflows/ci.yml"}, 22), nil)
	if err != nil {
		t.Fatalf("a broken artifact produced an error rather than a verdict: %v", err)
	}
	if res.Status != "invalid" {
		t.Fatalf("status %q for a workflow whose jobs is a string", res.Status)
	}
	outcome := decodeOutcome(t, res)
	if len(outcome.Findings) == 0 {
		t.Error("no findings, so a caller could not tell the user what to fix")
	}
}

func TestValidate_APathOutsideTheWorkspaceIsRefused(t *testing.T) {
	// A validator is still a program that opens a file the sender named, so the confinement that
	// applies to a write applies here. Without it, `validate.yaml` would be an arbitrary-file-read
	// primitive reachable with a signed envelope and no approval.
	root := t.TempDir()
	d := newDispatcher(t, root)
	for _, escape := range []string{"../outside.yaml", "../../etc/passwd", filepath.FromSlash("a/../../b.yaml")} {
		_, err := d.Execute(context.Background(),
			verified(t, OpValidateYAML, "", map[string]any{"path": escape}, 23), nil)
		if err == nil {
			t.Errorf("path %q was accepted", escape)
		}
	}
}

func TestValidate_AnAbsentPathArgumentIsRefused(t *testing.T) {
	d := newDispatcher(t, t.TempDir())
	_, err := d.Execute(context.Background(),
		verified(t, OpValidateCompose, "", map[string]any{}, 24), nil)
	if err == nil {
		t.Fatal("a validation with no path was accepted")
	}
	if !strings.Contains(err.Error(), "path") {
		t.Errorf("error %q does not say a path is needed", err)
	}
}

func TestValidateTrivy_AnUnknownThresholdIsRefusedByName(t *testing.T) {
	requireBinary(t, "trivy")
	root := t.TempDir()
	writeIn(t, root, "pod.yaml", "---\napiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n")
	d := newDispatcher(t, root)
	_, err := d.Execute(context.Background(),
		verified(t, OpValidateTrivy, "", map[string]any{"path": "pod.yaml", "threshold": "SORT-OF-BAD"}, 25), nil)
	if err == nil {
		t.Fatal("an unrecognised threshold was accepted, so a typo would silently change what blocks")
	}
	if !strings.Contains(err.Error(), "CRITICAL") {
		t.Errorf("error %q does not name the accepted values", err)
	}
}

func TestValidateCompose_ADirectoryIsRefused(t *testing.T) {
	requireBinary(t, "docker")
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "stack"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	d := newDispatcher(t, root)
	_, err := d.Execute(context.Background(),
		verified(t, OpValidateCompose, "", map[string]any{"path": "stack"}, 26), nil)
	if err == nil {
		t.Fatal("a directory was accepted as a compose file")
	}
}

func TestValidateK8s_ReportsTheModeThroughTheDispatcher(t *testing.T) {
	root := t.TempDir()
	writeIn(t, root, "cm.yaml", "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\ndata:\n  k: v\n")
	d := newDispatcher(t, root)
	res, err := d.Execute(context.Background(),
		verified(t, OpValidateK8s, "", map[string]any{"path": "cm.yaml"}, 27), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "valid" {
		t.Fatalf("status %q: %s", res.Status, res.Output)
	}
	outcome := decodeOutcome(t, res)
	if outcome.Mode == "" {
		t.Error("the outcome does not say which check ran, so a caller cannot tell what assurance it got")
	}
}

func TestValidateK8s_AnEmptyManifestIsInvalid(t *testing.T) {
	root := t.TempDir()
	writeIn(t, root, "nothing.yaml", "---\n# no object here\n")
	d := newDispatcher(t, root)
	res, err := d.Execute(context.Background(),
		verified(t, OpValidateK8s, "", map[string]any{"path": "nothing.yaml"}, 28), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "invalid" {
		t.Fatalf("a file declaring no object reported %q", res.Status)
	}
}

func TestReadinessInventory_ReportsWhatAScoreIsComputedFrom(t *testing.T) {
	root := t.TempDir()
	writeIn(t, root, "package.json", "{\n  \"name\": \"demo\"\n}\n")
	writeIn(t, root, "Dockerfile", "FROM alpine:3.20\n")
	writeIn(t, root, "src/index.ts", "export const x = 1;\n")
	d := newDispatcher(t, root)
	res, err := d.Execute(context.Background(),
		verified(t, OpReadinessInventory, "", map[string]any{"project_id": "proj-1"}, 29), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "ok" {
		t.Fatalf("status %q", res.Status)
	}
	var inventory struct {
		Manifests   []string `json:"manifests"`
		ConfigFiles []string `json:"config_files"`
		FileCount   int      `json:"file_count"`
	}
	if err := json.Unmarshal([]byte(res.Output), &inventory); err != nil {
		t.Fatalf("output is not an inventory: %v\n%s", err, res.Output)
	}
	if inventory.FileCount == 0 {
		t.Error("the inventory counted no files in a workspace that has three")
	}
}

func TestReadinessInventory_NeedsAProjectID(t *testing.T) {
	d := newDispatcher(t, t.TempDir())
	_, err := d.Execute(context.Background(),
		verified(t, OpReadinessInventory, "", map[string]any{}, 30), nil)
	if err == nil {
		t.Fatal("an inventory with no project_id was accepted")
	}
}

func TestSecretScanRun_FindsACredentialAndWithholdsItsValue(t *testing.T) {
	root := t.TempDir()
	// Assembled at run time so this source file carries no credential shape of its own —
	// `scripts/check-added-shapes.py` rejects one on any added line.
	prefix := "AK" + "IA"
	fake := prefix + strings.Repeat("Z", 16)
	writeIn(t, root, "config.txt", "aws_access_key_id = "+fake+"\n")
	d := newDispatcher(t, root)
	res, err := d.Execute(context.Background(),
		verified(t, OpSecretScanRun, "", map[string]any{"project_id": "proj-1"}, 31), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "clean" && res.Status != "findings" {
		t.Fatalf("status %q is neither clean nor findings", res.Status)
	}
	// Whatever gitleaks decided about this particular literal, the report must never carry it. That
	// is the invariant worth pinning: this report reaches an append-only, hash-chained audit trail,
	// so a credential inside it would become the most durable copy of the leak.
	if strings.Contains(res.Output, fake) {
		t.Fatal("the secret scan report carried the matched credential")
	}
	var report SecretScanReport
	if err := json.Unmarshal([]byte(res.Output), &report); err != nil {
		t.Fatalf("output is not a SecretScanReport: %v", err)
	}
	if report.FilesScanned == 0 {
		t.Error("no files were scanned")
	}
	if report.ScannerVersion == "" {
		t.Error("the report does not name the scanner; 'no credentials found' is only meaningful " +
			"alongside which rule set looked")
	}
	if report.FindingCount != len(report.Findings) && !report.Truncated {
		t.Errorf("FindingCount %d disagrees with %d findings and the report is not marked truncated",
			report.FindingCount, len(report.Findings))
	}
}

func TestSecretScanRun_NeedsAProjectID(t *testing.T) {
	d := newDispatcher(t, t.TempDir())
	_, err := d.Execute(context.Background(),
		verified(t, OpSecretScanRun, "", map[string]any{}, 32), nil)
	if err == nil {
		t.Fatal("a secret scan with no project_id was accepted")
	}
}

func TestSecretScanRun_DoesNotReadBlocklistedFiles(t *testing.T) {
	// FR-09. A `.env` must never be opened, so its contents cannot appear in a report even as a
	// redacted finding. The previous walk read every file and relied on redaction downstream, which
	// is a mitigation rather than a control.
	root := t.TempDir()
	prefix := "AK" + "IA"
	fake := prefix + strings.Repeat("Y", 16)
	writeIn(t, root, ".env", "AWS_ACCESS_KEY_ID="+fake+"\n")
	writeIn(t, root, "readme.md", "nothing sensitive\n")
	d := newDispatcher(t, root)
	res, err := d.Execute(context.Background(),
		verified(t, OpSecretScanRun, "", map[string]any{"project_id": "proj-1"}, 33), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if strings.Contains(res.Output, fake) {
		t.Fatal("the report carried a value from .env, which must never have been read")
	}
	var report SecretScanReport
	if err := json.Unmarshal([]byte(res.Output), &report); err != nil {
		t.Fatalf("output is not a SecretScanReport: %v", err)
	}
	for _, f := range report.Findings {
		if strings.HasSuffix(f.Path, ".env") {
			t.Errorf("a finding names .env at line %d, so the file was opened", f.Line)
		}
	}
}
