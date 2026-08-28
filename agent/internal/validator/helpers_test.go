// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

// newBuffer keeps the limitedWriter test readable without exporting anything from run.go.
func newBuffer() *bytes.Buffer { return &bytes.Buffer{} }

// The pure helpers, tested without a tool.
//
// These exist because the interesting failures in this package are not in the tool invocation — that
// either runs or does not — but in how a tool's output is turned into findings a user reads. A
// misparsed line number, a severity that silently becomes INFO, or a report that quietly carries a
// credential are all defects no amount of "does the binary exist" testing would find.

func TestReportable_TruncatesForTheFrameButNotForTheParser(t *testing.T) {
	// The two limits are separate for a reason: truncating before parsing broke `validate.trivy`,
	// because half a JSON document is unreadable rather than shorter.
	long := strings.Repeat("x", maxReportBytes+500)
	outcome := Outcome{Output: long}
	if len(outcome.Output) != maxReportBytes+500 {
		t.Fatal("the outcome under test was already truncated")
	}
	reported := outcome.Reportable()
	if len(reported.Output) <= maxReportBytes {
		t.Errorf("Reportable() produced %d bytes; expected the cap plus a note", len(reported.Output))
	}
	if !strings.Contains(reported.Output, "omitted from the report") {
		t.Error("a truncated report does not say that it was truncated")
	}
	// And the original is untouched, so a validator that parses after reporting still can.
	if len(outcome.Output) != maxReportBytes+500 {
		t.Error("Reportable() mutated the outcome it was called on")
	}
	short := Outcome{Output: "fine"}
	if short.Reportable().Output != "fine" {
		t.Error("a short output was altered")
	}
}

func TestLimitedWriter_StopsAtTheCeilingAndSaysSo(t *testing.T) {
	var sink = &limitedWriter{w: newBuffer(), remaining: 10}
	n, err := sink.Write([]byte("0123456789ABCDEF"))
	if err != nil {
		t.Fatalf("write: %v", err)
	}
	// The full length is reported to the caller: a short write would make the child process see a
	// broken pipe and change its behaviour, which is not the intent.
	if n != 16 {
		t.Errorf("Write reported %d, want 16", n)
	}
	if !sink.truncated {
		t.Error("the writer did not record that it truncated")
	}
	if got := sink.w.String(); got != "0123456789" {
		t.Errorf("retained %q, want the first 10 bytes", got)
	}
	// A further write past the ceiling is discarded rather than growing the buffer.
	if _, err := sink.Write([]byte("more")); err != nil {
		t.Fatalf("second write: %v", err)
	}
	if got := sink.w.String(); got != "0123456789" {
		t.Errorf("a write past the ceiling was retained: %q", got)
	}
}

func TestFindingsFromLines_NeverReturnsNothingForAFailure(t *testing.T) {
	// A non-zero exit with no diagnostic output still has to become a finding, or a caller
	// thresholding on findings would read the failure as a pass.
	findings := findingsFromLines("", SeverityHigh, "some-rule", "a/path")
	if len(findings) != 1 {
		t.Fatalf("silent failure produced %d findings, want 1", len(findings))
	}
	if findings[0].Rule != "some-rule" || findings[0].Path != "a/path" {
		t.Errorf("the synthesised finding lost its rule or path: %+v", findings[0])
	}

	findings = findingsFromLines("first problem\n\nsecond problem\n", SeverityMedium, "r", "p")
	if len(findings) != 2 {
		t.Fatalf("got %d findings from two non-empty lines: %+v", len(findings), findings)
	}
	for _, f := range findings {
		if f.Severity != SeverityMedium {
			t.Errorf("severity %q was not the one requested", f.Severity)
		}
	}
	// The truncation note is not a diagnostic and must not become a finding.
	findings = findingsFromLines("[output truncated at 99 bytes]", SeverityHigh, "r", "p")
	for _, f := range findings {
		if strings.Contains(f.Message, "truncated") {
			t.Error("the truncation note became a finding")
		}
	}
}

func TestYamllintFindings_ParsesLocationsIncludingWindowsPaths(t *testing.T) {
	// `-f parsable` gives `path:line:col: [level] message`. A Windows path carries a drive colon, so
	// naive splitting on the first colon puts the line number where the path belongs — and the agent
	// ships for windows/amd64 and windows/arm64.
	unix := yamllintFindings("charts/app/values.yaml:7:3: [error] duplication of key (key-duplicates)", "fallback")
	if len(unix) != 1 || unix[0].Line != 7 || unix[0].Path != "charts/app/values.yaml" {
		t.Errorf("unix path parsed as %+v", unix)
	}
	if unix[0].Severity != SeverityHigh {
		t.Errorf("an [error] became %q rather than HIGH", unix[0].Severity)
	}
	win := yamllintFindings(`C:\work\repo\values.yaml:12:1: [warning] too many blank lines (empty-lines)`, "fallback")
	if len(win) != 1 {
		t.Fatalf("windows path produced %d findings", len(win))
	}
	if win[0].Line != 12 {
		t.Errorf("windows path parsed line %d, want 12 (path %q)", win[0].Line, win[0].Path)
	}
	// An unparsable line still becomes a finding, attributed to the file being checked, rather than
	// being dropped.
	odd := yamllintFindings("something unexpected", "the/file.yaml")
	if len(odd) != 1 || odd[0].Path != "the/file.yaml" {
		t.Errorf("unparsable output produced %+v", odd)
	}
	if got := yamllintFindings("", "f"); len(got) != 1 {
		t.Errorf("empty output produced %d findings, want a synthesised one", len(got))
	}
}

func TestNormaliseSeverity_MapsTrivysVocabularyAndDefaultsSafely(t *testing.T) {
	cases := map[string]Severity{
		"CRITICAL": SeverityCritical, "critical": SeverityCritical,
		"HIGH": SeverityHigh, " high ": SeverityHigh,
		"MEDIUM": SeverityMedium, "LOW": SeverityLow,
		"UNKNOWN": SeverityInfo, "": SeverityInfo,
	}
	for input, want := range cases {
		if got := normaliseSeverity(input); got != want {
			t.Errorf("normaliseSeverity(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestSummariseTrivy_CountsBySeverity(t *testing.T) {
	if got := summariseTrivy(nil); !strings.Contains(got, "no misconfigurations") {
		t.Errorf("an empty scan summarised as %q", got)
	}
	summary := summariseTrivy([]Finding{
		{Severity: SeverityHigh}, {Severity: SeverityHigh}, {Severity: SeverityLow},
	})
	if !strings.Contains(summary, "HIGH=2") || !strings.Contains(summary, "LOW=1") {
		t.Errorf("summary %q does not count by severity", summary)
	}
}

func TestTrivyFindings_EmptyOutputIsNoFindingsAndBadOutputIsAnError(t *testing.T) {
	// Under `--quiet` Trivy prints nothing at all for a target with no results, which must read as
	// "clean" rather than as a parse failure.
	findings, err := trivyFindings("   \n", "target")
	if err != nil {
		t.Fatalf("empty output errored: %v", err)
	}
	if len(findings) != 0 {
		t.Errorf("empty output produced %d findings", len(findings))
	}
	// Unreadable output is an error, never a pass: a scanner whose output cannot be read has
	// established nothing.
	if _, err := trivyFindings("{not json", "target"); err == nil {
		t.Error("unreadable scanner output was accepted")
	}
}

func TestTrivyFindings_WithholdsSecretValuesAndKeepsCoordinates(t *testing.T) {
	report := `{"Results":[{"Target":"app/config.yaml",
      "Misconfigurations":[{"ID":"KSV017","Title":"Privileged","Message":"container is privileged",
        "Severity":"HIGH","CauseMetadata":{"StartLine":11}}],
      "Secrets":[{"RuleID":"aws-access-key","Title":"AWS Access Key","Severity":"CRITICAL","StartLine":4}]}]}`
	findings, err := trivyFindings(report, "fallback")
	if err != nil {
		t.Fatalf("parsing: %v", err)
	}
	if len(findings) != 2 {
		t.Fatalf("got %d findings, want 2: %+v", len(findings), findings)
	}
	var sawSecret bool
	for _, f := range findings {
		if f.Path != "app/config.yaml" {
			t.Errorf("finding lost its target path: %+v", f)
		}
		if strings.HasPrefix(f.Rule, "secret:") {
			sawSecret = true
			if f.Severity != SeverityCritical || f.Line != 4 {
				t.Errorf("secret finding lost severity or line: %+v", f)
			}
			if !strings.Contains(f.Message, "value withheld") {
				t.Errorf("a secret finding does not state that the value is withheld: %q", f.Message)
			}
		}
	}
	if !sawSecret {
		t.Error("the secret finding was dropped")
	}
}

func TestFirstLine(t *testing.T) {
	if got := firstLine("v1.2.3\nextra\n"); got != "v1.2.3" {
		t.Errorf("firstLine = %q", got)
	}
	if got := firstLine("  only  "); got != "only" {
		t.Errorf("firstLine trimmed to %q", got)
	}
	if got := firstLine(""); got != "" {
		t.Errorf("firstLine of empty = %q", got)
	}
}

func TestYamllintConfigIsEmbeddedAndUsable(t *testing.T) {
	// The config travels inside the binary because the agent is installed on a machine that does not
	// have this repository. An empty embed would silently fall back to yamllint's default rules,
	// whose `truthy` check rejects every valid GitHub Actions workflow.
	if len(yamllintConfig) == 0 {
		t.Fatal("the yamllint config did not embed")
	}
	if !strings.Contains(string(yamllintConfig), "check-keys: false") {
		t.Error("the embedded config lacks the truthy exemption that lets a workflow's `on:` key pass")
	}
	path, err := yamllintConfigFile()
	if err != nil {
		t.Fatalf("staging the config: %v", err)
	}
	again, err := yamllintConfigFile()
	if err != nil {
		t.Fatalf("second call: %v", err)
	}
	if path != again {
		t.Error("the config was staged twice; it should be written once per process")
	}
}

func TestSchemaCompile_RejectsABrokenSchema(t *testing.T) {
	if _, err := CompileSchema("broken", []byte("{not json")); err == nil {
		t.Error("a malformed schema compiled")
	}
}

func TestCheckAllDocuments_ChecksEveryDocumentAndRejectsAnEmptyFile(t *testing.T) {
	dir := t.TempDir()
	// The second document is the bad one. A checker that read only the first would call this valid,
	// and a multi-object manifest is the normal case for Kubernetes.
	multi := writeFile(t, dir, "multi.yaml", `---
apiVersion: v1
kind: ConfigMap
metadata:
  name: good
---
apiVersion: v1
kind: ConfigMap
metadata: {}
`)
	findings, err := checkAllDocuments(multi, k8sManifestSchema)
	if err != nil {
		t.Fatalf("checking: %v", err)
	}
	if len(findings) == 0 {
		t.Fatal("the second document's missing metadata.name was not reported")
	}
	if !strings.Contains(findings[0].Message, "document 2") {
		t.Errorf("the finding does not say which document it is about: %q", findings[0].Message)
	}

	empty := writeFile(t, dir, "empty.yaml", "---\n# nothing\n")
	findings, err = checkAllDocuments(empty, k8sManifestSchema)
	if err != nil {
		t.Fatalf("checking an empty file: %v", err)
	}
	if len(findings) == 0 {
		t.Error("a file declaring no object was accepted")
	}

	broken := writeFile(t, dir, "broken.yaml", "apiVersion: v1\n\tkind: bad tab\n")
	if _, err := checkAllDocuments(broken, k8sManifestSchema); err == nil {
		t.Error("unparsable YAML was reported as a schema problem rather than as unreadable")
	}
}

func TestRunner_InSharesTheVersionCacheWithoutCopyingALock(t *testing.T) {
	// `Runner` holds a mutex, so `In` must not be a struct copy — that is both a vet error and a real
	// bug, since two copies would guard two caches while looking like they guard one.
	base := &Runner{Dir: t.TempDir(), Env: []string{"FORGEOPS_TEST=1"}}
	scoped := base.In("elsewhere")
	if scoped.Dir != "elsewhere" {
		t.Errorf("In() did not set the directory: %q", scoped.Dir)
	}
	if base.Dir == "elsewhere" {
		t.Error("In() mutated the receiver")
	}
	if len(scoped.Env) != 1 || scoped.Env[0] != "FORGEOPS_TEST=1" {
		t.Errorf("In() lost the environment: %v", scoped.Env)
	}
	if base.sharedCache() != scoped.cache {
		t.Error("In() did not share the version cache, so a version would be probed twice")
	}
}

func TestRunner_LookNamesTheMissingTool(t *testing.T) {
	if _, err := (&Runner{}).Look("forgeops-definitely-not-a-tool"); err == nil {
		t.Fatal("a missing tool resolved")
	} else if !strings.Contains(err.Error(), "forgeops-definitely-not-a-tool") {
		t.Errorf("error %q does not name the tool", err)
	}
	// A tool that certainly exists: the test binary itself is not on PATH, so use the Go toolchain,
	// which must be present for this test to be running at all.
	if _, err := (&Runner{}).Look("go"); err != nil {
		t.Errorf("go was not found on PATH: %v", err)
	}
}

func TestRunner_VersionIsCachedAndNeverEmpty(t *testing.T) {
	runner := &Runner{Dir: t.TempDir()}
	// A tool that does not exist still yields a usable string rather than an empty one, because an
	// Outcome always names a version and "" would read as a missing field rather than a failed probe.
	got := runner.Version(ctxFor(t), "forgeops-definitely-not-a-tool", "--version")
	if got == "" {
		t.Fatal("Version returned an empty string")
	}
	if again := runner.Version(ctxFor(t), "forgeops-definitely-not-a-tool", "--version"); again != got {
		t.Errorf("Version was not cached: %q then %q", got, again)
	}
}

func TestSchemaCheck_DistinguishesUnreadableFromWrongShape(t *testing.T) {
	// The two must not collapse: "the file is not YAML" is an error the caller reports differently
	// from "the document is the wrong shape", which is a finding a user can act on.
	if _, err := helmChartSchema.Check(filepath.Join(t.TempDir(), "absent.yaml")); err == nil {
		t.Error("a missing file produced no error")
	}
	dir := t.TempDir()
	badYAML := writeFile(t, dir, "bad.yaml", "name: x\n\tversion: tab\n")
	if _, err := helmChartSchema.Check(badYAML); err == nil {
		t.Error("unparsable YAML was reported as a schema violation")
	}
	wrongShape := writeFile(t, dir, "Chart.yaml", "---\napiVersion: v2\nname: x\nversion: not-semver\n")
	findings, err := helmChartSchema.Check(wrongShape)
	if err != nil {
		t.Fatalf("a well-formed document with a wrong field errored: %v", err)
	}
	if len(findings) == 0 {
		t.Error("a non-SemVer chart version was accepted; Helm requires SemVer 2 rather than preferring it")
	}
}
