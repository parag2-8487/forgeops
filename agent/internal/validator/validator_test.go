// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// These tests run the REAL tools. That is the whole point of the package they cover.
//
// The predecessor's tests passed a string containing `apiVersion:` and asserted no error, which is a
// test of `strings.Contains`. Nothing here asserts on the validator's own opinion: every case writes
// a real artifact to disk, runs the real binary, and checks what the binary said. A pair per
// validator — one artifact that must pass and one that must fail — because a validator that returns
// "valid" unconditionally passes every positive test ever written, and that is precisely the defect
// this package was rebuilt to remove.
//
// `requireTool` skips when a binary is absent so a developer without the full toolchain can still run
// the rest of the suite. CI installs all of them, and `scripts/check-no-skips.py --go` fails the
// build if any of these skip there — so "it passed in CI" cannot mean "it did not run in CI".

func requireTool(t *testing.T, tool string) {
	t.Helper()
	if _, err := exec.LookPath(tool); err != nil {
		t.Skipf("%s not found", tool)
	}
}

func testRunner(t *testing.T, dir string) *Runner {
	t.Helper()
	return &Runner{Dir: dir}
}

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
	return path
}

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	t.Cleanup(cancel)
	return ctx
}

// ── the built-in schemas ──────────────────────────────────────────────────────────────────────

func TestBuiltInSchemasCompile(t *testing.T) {
	// `mustCompile` panics at package initialisation on a broken schema, which would make every
	// validation in the binary meaningless. Reaching this line at all proves both compiled; the
	// assertions below prove they are the schemas intended rather than empty ones that accept
	// anything, which would compile just as happily.
	if githubWorkflowSchema == nil || githubWorkflowSchema.Name != "github-workflow" {
		t.Fatal("the GitHub workflow schema is absent or misnamed")
	}
	if helmChartSchema == nil || helmChartSchema.Name != "helm-chart" {
		t.Fatal("the Helm chart schema is absent or misnamed")
	}
	dir := t.TempDir()
	empty := writeFile(t, dir, "empty.yaml", "{}\n")
	findings, err := githubWorkflowSchema.Check(empty)
	if err != nil {
		t.Fatalf("checking an empty document: %v", err)
	}
	if len(findings) == 0 {
		t.Error("the workflow schema accepted an empty document, so it constrains nothing")
	}
}

func TestSchemaFor_ChoosesByLocation(t *testing.T) {
	cases := map[string]string{
		".github/workflows/ci.yml":      "github-workflow",
		"charts/app/Chart.yaml":         "helm-chart",
		"Chart.yaml":                    "helm-chart",
		"docker-compose.yml":            "",
		"k8s/deployment.yaml":           "",
		".github/workflows/deploy.yaml": "github-workflow",
	}
	for path, want := range cases {
		schema := SchemaFor(path)
		got := ""
		if schema != nil {
			got = schema.Name
		}
		if got != want {
			t.Errorf("SchemaFor(%q) = %q, want %q", path, got, want)
		}
	}
	// Windows separators must resolve identically, or the same repository validates differently by
	// platform — the agent ships for windows/amd64 and windows/arm64.
	if SchemaFor(`.github\workflows\ci.yml`) == nil {
		t.Error("a backslash-separated workflow path found no schema")
	}
}

// ── validate.yaml ─────────────────────────────────────────────────────────────────────────────

// : A workflow that is valid YAML, satisfies the schema, and would actually run.
const goodWorkflow = `---
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
`

// : Valid YAML, parses cleanly, and GitHub would reject it: `jobs` is a string, and the step list is a
// : mapping. A key-presence check accepts this, which is why the predecessor's `requiredKeys` approach
// : could not do the job.
const structurallyWrongWorkflow = `---
name: ci
on:
  push:
    branches: [main]
jobs: "build it please"
`

func TestValidateYAML_AcceptsAWorkflowThatWouldRun(t *testing.T) {
	requireTool(t, "yamllint")
	dir := t.TempDir()
	path := writeFile(t, dir, ".github/workflows/ci.yml", goodWorkflow)
	outcome, err := testRunner(t, dir).ValidateYAML(ctxFor(t), path, SchemaFor(".github/workflows/ci.yml"))
	if err != nil {
		t.Fatalf("validate: %v", err)
	}
	if !outcome.Passed {
		t.Fatalf("a runnable workflow was rejected: %s\n%v", outcome.Output, outcome.Findings)
	}
	if outcome.ToolVersion == "" || outcome.ToolVersion == "unknown" {
		t.Errorf("the outcome does not name the yamllint version (%q); a pass from an unknown "+
			"version is not evidence", outcome.ToolVersion)
	}
	if !strings.Contains(outcome.Mode, "schema:github-workflow") {
		t.Errorf("mode %q does not record that the schema was applied", outcome.Mode)
	}
}

func TestValidateYAML_CatchesAWorkflowThatParsesAndCannotRun(t *testing.T) {
	requireTool(t, "yamllint")
	dir := t.TempDir()
	path := writeFile(t, dir, ".github/workflows/ci.yml", structurallyWrongWorkflow)
	outcome, err := testRunner(t, dir).ValidateYAML(ctxFor(t), path, SchemaFor(".github/workflows/ci.yml"))
	if err != nil {
		t.Fatalf("validate: %v", err)
	}
	if outcome.Passed {
		t.Fatal("a workflow whose `jobs` is a string was reported valid; the schema is not being applied")
	}
	if outcome.ExitCode == 0 {
		t.Error("a failed validation left ExitCode at 0, so a caller reading only the status sees a pass")
	}
	var sawSchemaFinding bool
	for _, f := range outcome.Findings {
		if strings.HasPrefix(f.Rule, "schema:") {
			sawSchemaFinding = true
		}
	}
	if !sawSchemaFinding {
		t.Errorf("no schema finding among %v; yamllint alone cannot know this file is wrong", outcome.Findings)
	}
}

func TestValidateYAML_ReportsRealCoordinatesForALintFinding(t *testing.T) {
	requireTool(t, "yamllint")
	dir := t.TempDir()
	// A duplicate key: well-formed enough to parse, and an error yamllint locates precisely.
	path := writeFile(t, dir, "values.yaml", "---\nreplicas: 1\nreplicas: 2\n")
	outcome, err := testRunner(t, dir).ValidateYAML(ctxFor(t), path, nil)
	if err != nil {
		t.Fatalf("validate: %v", err)
	}
	if outcome.Passed {
		t.Fatal("a duplicate mapping key was accepted")
	}
	var located bool
	for _, f := range outcome.Findings {
		if f.Line > 0 {
			located = true
		}
	}
	if !located {
		t.Errorf("no finding carried a line number: %v", outcome.Findings)
	}
}

// ── validate.compose ──────────────────────────────────────────────────────────────────────────

func TestValidateCompose_AcceptsAValidFileAndRejectsABrokenOne(t *testing.T) {
	requireTool(t, "docker")
	dir := t.TempDir()
	good := writeFile(t, dir, "docker-compose.yml", `---
services:
  web:
    image: nginx:1.27-alpine
    ports:
      - "8080:80"
`)
	outcome, err := testRunner(t, dir).ValidateCompose(ctxFor(t), good)
	if err != nil {
		t.Fatalf("validating a good compose file: %v", err)
	}
	if !outcome.Passed {
		t.Fatalf("a valid compose file was rejected: %s", outcome.Output)
	}

	// `ports` as a mapping is a schema violation `docker compose config` reports and a substring
	// check cannot: every token a naive validator looks for is present.
	bad := writeFile(t, dir, "broken-compose.yml", `---
services:
  web:
    image: nginx:1.27-alpine
    ports:
      host: 8080
      container: 80
`)
	broken, err := testRunner(t, dir).ValidateCompose(ctxFor(t), bad)
	if err != nil {
		t.Fatalf("validating a broken compose file: %v", err)
	}
	if broken.Passed {
		t.Fatal("a compose file with a malformed `ports` was reported valid")
	}
	if len(broken.Findings) == 0 {
		t.Error("a failed compose validation produced no findings, so a caller thresholding on " +
			"findings would treat it as a pass")
	}
}

// ── validate.k8s ──────────────────────────────────────────────────────────────────────────────

func TestValidateK8s_NamesWhichDryRunItPerformed(t *testing.T) {
	requireTool(t, "kubectl")
	dir := t.TempDir()
	path := writeFile(t, dir, "deployment.yaml", `---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.27-alpine
`)
	outcome, err := testRunner(t, dir).ValidateK8s(ctxFor(t), path)
	if err != nil {
		t.Fatalf("a valid Deployment could not be dry-run at all: %v", err)
	}
	if !outcome.Passed {
		t.Fatalf("a valid Deployment was rejected in %q: %s", outcome.Mode, outcome.Output)
	}
	// The mode must say which assurance was obtained, and the no-cluster mode must say plainly that
	// schema validation did not happen. A reader who is told only "client-dry-run" would reasonably
	// assume the manifest had been checked against a schema.
	switch {
	case strings.HasPrefix(outcome.Mode, "server-dry-run"):
		// Nothing more to assert: the cluster applied its own schema and admission.
	case strings.HasPrefix(outcome.Mode, "local-schema"):
		if !strings.Contains(outcome.Mode, "did NOT run") {
			t.Errorf("mode %q does not disclose that no schema was applied", outcome.Mode)
		}
	default:
		t.Fatalf("mode %q names neither a server dry run nor the local-schema fallback", outcome.Mode)
	}
}

func TestValidateK8s_RejectsADocumentThatProducesNoObjects(t *testing.T) {
	requireTool(t, "kubectl")
	dir := t.TempDir()
	// Parses as YAML, produces no Kubernetes object. kubectl exits zero over it, so a validator that
	// trusted the exit code alone would call this a valid manifest.
	path := writeFile(t, dir, "empty.yaml", "---\n# nothing here\n")
	outcome, err := testRunner(t, dir).ValidateK8s(ctxFor(t), path)
	if err != nil {
		t.Fatalf("dry run: %v", err)
	}
	if outcome.Passed {
		t.Fatal("a document with no apiVersion or kind was reported as a valid manifest")
	}
}

func TestValidateK8s_UnknownFieldIsCaughtWhereItCanBe(t *testing.T) {
	requireTool(t, "kubectl")
	dir := t.TempDir()
	path := writeFile(t, dir, "bad.yaml", `---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
dataz:
  key: value
`)
	outcome, err := testRunner(t, dir).ValidateK8s(ctxFor(t), path)
	if err != nil {
		t.Fatalf("dry run: %v", err)
	}
	if strings.HasPrefix(outcome.Mode, "server-dry-run") {
		if outcome.Passed {
			t.Fatal("a ConfigMap with an unknown `dataz` field passed a strict server dry run")
		}
		return
	}
	// Without a cluster there is no schema to catch `dataz` against, and this test must not pretend
	// otherwise — asserting a failure here would pass for the wrong reason (kubectl erroring on an
	// OpenAPI download), which is how a test starts certifying the opposite of its name. What must
	// hold is that the result does not claim an assurance it did not obtain.
	if !strings.Contains(outcome.Mode, "did NOT run") {
		t.Errorf("without a cluster the mode must disclose what did not run; got %q", outcome.Mode)
	}
}

// ── validate.helm ─────────────────────────────────────────────────────────────────────────────

func writeChart(t *testing.T, dir, template string) string {
	t.Helper()
	chart := filepath.Join(dir, "mychart")
	writeFile(t, chart, "Chart.yaml", "---\napiVersion: v2\nname: mychart\nversion: 0.1.0\n")
	writeFile(t, chart, "values.yaml", "---\nreplicas: 1\n")
	writeFile(t, chart, filepath.Join("templates", "cm.yaml"), template)
	return chart
}

func TestValidateHelm_AcceptsAChartThatRenders(t *testing.T) {
	requireTool(t, "helm")
	chart := writeChart(t, t.TempDir(), `---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cm
data:
  replicas: "{{ .Values.replicas }}"
`)
	outcome, err := testRunner(t, filepath.Dir(chart)).ValidateHelm(ctxFor(t), chart)
	if err != nil {
		t.Fatalf("validating a good chart: %v", err)
	}
	if !outcome.Passed {
		t.Fatalf("a renderable chart was rejected: %s\n%v", outcome.Output, outcome.Findings)
	}
	if !strings.Contains(outcome.Mode, "template") {
		t.Errorf("mode %q does not record that the templates were rendered; `lint` alone cannot "+
			"catch a template error", outcome.Mode)
	}
}

func TestValidateHelm_CatchesAChartThatLintsAndCannotRender(t *testing.T) {
	requireTool(t, "helm")
	// `.Values.missing.deeper` is a nil-map dereference at render time. `helm lint` alone is happy
	// with the chart's structure, so this is the case that makes the template step necessary.
	chart := writeChart(t, t.TempDir(), `---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cm
data:
  value: "{{ .Values.missing.deeper }}"
`)
	outcome, err := testRunner(t, filepath.Dir(chart)).ValidateHelm(ctxFor(t), chart)
	if err != nil {
		t.Fatalf("validating a broken chart: %v", err)
	}
	if outcome.Passed {
		t.Fatal("a chart that cannot render was reported valid")
	}
}

// ── validate.tofu ─────────────────────────────────────────────────────────────────────────────

func TestValidateTofu_AcceptsAValidModuleAndRejectsABrokenOne(t *testing.T) {
	requireTool(t, "tofu")
	goodDir := t.TempDir()
	writeFile(t, goodDir, "main.tf", `
variable "name" {
  type    = string
  default = "demo"
}

output "name" {
  value = var.name
}
`)
	outcome, err := testRunner(t, goodDir).ValidateTofu(ctxFor(t), goodDir)
	if err != nil {
		t.Fatalf("validating a good module: %v", err)
	}
	if !outcome.Passed {
		t.Fatalf("a valid module was rejected in mode %q: %s", outcome.Mode, outcome.Output)
	}
	if !strings.Contains(outcome.Mode, "validate") {
		t.Errorf("mode %q does not record that validate ran", outcome.Mode)
	}

	badDir := t.TempDir()
	// References an undeclared variable. The word `resource` is absent and the word `output` is
	// present, so the predecessor's check would have passed this.
	writeFile(t, badDir, "main.tf", `
output "name" {
  value = var.undeclared_everywhere
}
`)
	broken, err := testRunner(t, badDir).ValidateTofu(ctxFor(t), badDir)
	if err != nil {
		t.Fatalf("validating a broken module: %v", err)
	}
	if broken.Passed {
		t.Fatal("a module referencing an undeclared variable was reported valid")
	}
	var located bool
	for _, f := range broken.Findings {
		if f.Line > 0 && strings.HasSuffix(f.Path, ".tf") {
			located = true
		}
	}
	if !located {
		t.Errorf("no finding carried a .tf path and line: %v", broken.Findings)
	}
}

// ── validate.trivy ────────────────────────────────────────────────────────────────────────────

func TestValidateTrivy_FindsAMisconfigurationAndHonoursTheThreshold(t *testing.T) {
	requireTool(t, "trivy")
	dir := t.TempDir()
	// A privileged container: a real Trivy misconfiguration rule, not a substring the predecessor
	// happened to look for.
	writeFile(t, dir, "pod.yaml", `---
apiVersion: v1
kind: Pod
metadata:
  name: bad
spec:
  containers:
    - name: app
      image: nginx:1.27-alpine
      securityContext:
        privileged: true
        allowPrivilegeEscalation: true
        runAsUser: 0
`)
	runner := testRunner(t, dir)
	outcome, err := runner.ValidateTrivy(ctxFor(t), dir, SeverityHigh)
	if err != nil {
		t.Fatalf("scanning: %v", err)
	}
	if len(outcome.Findings) == 0 {
		t.Fatalf("trivy found nothing in a privileged pod spec; output: %s", outcome.Output)
	}
	if outcome.Passed {
		t.Error("a HIGH-or-worse finding did not fail the HIGH threshold")
	}
	// The same scan under a threshold nothing reaches must pass, which is what proves the threshold
	// is doing the deciding rather than the presence of any finding at all.
	lenient, err := runner.ValidateTrivy(ctxFor(t), dir, SeverityCritical)
	if err != nil {
		t.Fatalf("rescanning: %v", err)
	}
	if worst, found := lenient.Worst(); found && severityRank[worst] < severityRank[SeverityCritical] && !lenient.Passed {
		t.Errorf("worst finding is %s but the CRITICAL threshold still failed", worst)
	}
}

func TestValidateTrivy_NeverCarriesTheSecretItFound(t *testing.T) {
	requireTool(t, "trivy")
	dir := t.TempDir()
	// Assembled at run time so this source file does not itself carry a credential shape —
	// `scripts/check-added-shapes.py` rejects one on any added line, and it is right to.
	prefix := "AK" + "IA"
	fake := prefix + strings.Repeat("Q", 16)
	writeFile(t, dir, "leak.env.txt", "AWS_ACCESS_KEY_ID="+fake+"\n")
	outcome, err := testRunner(t, dir).ValidateTrivy(ctxFor(t), dir, SeverityLow)
	if err != nil {
		t.Fatalf("scanning: %v", err)
	}
	// Whether Trivy's rules match this particular shape is its business; what must hold is that if
	// it reports something, the value is not in the report.
	for _, f := range outcome.Findings {
		if strings.Contains(f.Message, fake) {
			t.Fatalf("a finding carried the matched credential: %q", f.Message)
		}
	}
	if strings.Contains(outcome.Output, fake) {
		t.Fatal("the summarised output carried the matched credential")
	}
}

// ── the runner itself ─────────────────────────────────────────────────────────────────────────

func TestRunner_AMissingToolIsAFailureAndNeverAPass(t *testing.T) {
	runner := &Runner{Dir: t.TempDir()}
	_, err := runner.Run(ctxFor(t), "forgeops-no-such-tool-exists", "--version")
	if err == nil {
		t.Fatal("a missing tool produced no error, so an unverifiable artifact would read as verified")
	}
	if !strings.Contains(err.Error(), "tool not found") {
		t.Errorf("error %q does not say the tool was missing", err)
	}
}

func TestRunner_ANonZeroExitIsAVerdictNotAnError(t *testing.T) {
	// The distinction the generation pipeline depends on: "the artifact is invalid" must be a
	// completed run with Passed=false, while "we could not check" must be an error. Collapsing them
	// is how a validator starts reporting a pass it did not establish.
	requireTool(t, "yamllint")
	dir := t.TempDir()
	path := writeFile(t, dir, "bad.yaml", "---\na: 1\na: 2\n")
	outcome, err := (&Runner{Dir: dir}).Run(ctxFor(t), "yamllint", "-f", "parsable", "--strict", path)
	if err != nil {
		t.Fatalf("a tool that ran and objected returned an error: %v", err)
	}
	if outcome.Passed || outcome.ExitCode == 0 {
		t.Errorf("a duplicate-key document reported Passed=%v ExitCode=%d", outcome.Passed, outcome.ExitCode)
	}
	if outcome.Output == "" {
		t.Error("the tool's own diagnostic text did not travel, which is the useful part of a failure")
	}
}

func TestRunner_TheContextBoundsTheTool(t *testing.T) {
	requireTool(t, "yamllint")
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // already done before the tool starts
	_, err := (&Runner{Dir: t.TempDir()}).Run(ctx, "yamllint", "--version")
	if err == nil {
		t.Fatal("a cancelled context produced a verdict; a bounded operation must not report one")
	}
}

func TestOutcome_WorstOrdersSeverities(t *testing.T) {
	outcome := Outcome{Findings: []Finding{
		{Severity: SeverityLow}, {Severity: SeverityCritical}, {Severity: SeverityMedium},
	}}
	worst, found := outcome.Worst()
	if !found || worst != SeverityCritical {
		t.Errorf("Worst() = %q, %v; want CRITICAL, true", worst, found)
	}
	if _, found := (Outcome{}).Worst(); found {
		t.Error("an empty outcome reported a worst finding")
	}
}
