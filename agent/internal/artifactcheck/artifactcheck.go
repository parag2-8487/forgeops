// SPDX-License-Identifier: Apache-2.0

// Package artifactcheck runs the real external validators over the artifacts a repository ALREADY has,
// so the readiness score can report what the tools said rather than whether a path existed.
//
// WHY THIS EXISTS
// ---------------
// Six genuine validators live in internal/validator, every one of them shelling out to a pinned binary
// and reporting what it actually said. All six only ever judged GENERATED files: the backend's
// `validations` table is keyed by `change_item_id`, so a file the user wrote themselves could never
// appear in it, and the table had no rows at all.
//
// The consequence was a readiness report that scored a hand-written `docker-compose.yml` on whether the
// path existed, while the tool that could have said "this does not parse" sat unused on the same machine
// the scan was running on. The score said one thing and `docker compose config` said another.
//
// WHAT IS NOT RUN HERE, AND WHY THAT IS A DECISION AND NOT AN OMISSION
// --------------------------------------------------------------------
// Only the FAST, LOCAL, READ-ONLY validators run during a scan: compose, kubeconform, helm lint and
// yamllint/schema. `ValidateTofu` requires `tofu init`, which reaches the network and writes to the
// module directory, and `ValidateTrivy` downloads a vulnerability database. Neither belongs in a scan a
// user expects to finish in seconds, and neither may be run implicitly against their tree.
//
// Those two are therefore reported as NOT RUN rather than passed. A scan that silently omitted them would
// be indistinguishable from a scan where they passed, which is exactly the fabrication the validator
// package was written to remove.
package artifactcheck

import (
	"context"
	"errors"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/scanner"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

// PerArtifactTimeout bounds one tool invocation.
//
// A scan is interactive. A validator that hangs on a pathological file must not hold the whole report,
// and a timeout is reported as `errored` — which is true, and distinguishable from a pass.
const PerArtifactTimeout = 30 * time.Second

// MaxArtifacts caps how many files are validated in one scan.
//
// A monorepo can hold hundreds of manifests, and 300 kubeconform invocations would turn a scan into a
// build. The cap is applied per KIND so a repository with many manifests and one compose file still gets
// its compose file checked.
const MaxArtifacts = 25

// Status values. Four, because "the tool says this is fine", "the tool says it is broken", "the tool is
// not installed" and "the tool could not run" are four different facts.
const (
	StatusPassed      = "passed"
	StatusFailed      = "failed"
	StatusToolMissing = "tool_missing"
	StatusErrored     = "errored"
)

// Kinds of artifact this package knows how to validate.
const (
	KindCompose = "compose"
	KindK8s     = "k8s"
	KindHelm    = "helm"
	KindYAML    = "yaml_schema"
)

// Run validates the artifacts among `paths` and returns one entry per artifact attempted.
//
// `root` is the workspace root; `paths` are repository-relative. The returned slice is ordered by path so
// two scans of one tree produce the same report — the determinism the inventory hash depends on.
func Run(ctx context.Context, root string, paths []string) []scanner.ScanValidation {
	runner := &validator.Runner{Dir: root}
	var out []scanner.ScanValidation

	for _, group := range classify(paths) {
		for _, rel := range group.paths {
			abs := filepath.Join(root, filepath.FromSlash(rel))
			out = append(out, validateOne(ctx, runner, group.kind, rel, abs))
		}
	}
	return out
}

type artifactGroup struct {
	kind  string
	paths []string
}

// classify sorts the repository's paths into the artifact kinds this package can validate.
//
// The ORDER of the returned groups is fixed rather than map iteration order, because a report whose
// entries reorder between two identical scans is not deterministic even when its contents match.
func classify(paths []string) []artifactGroup {
	groups := []artifactGroup{
		{kind: KindCompose},
		{kind: KindK8s},
		{kind: KindHelm},
		{kind: KindYAML},
	}
	index := map[string]int{KindCompose: 0, KindK8s: 1, KindHelm: 2, KindYAML: 3}

	for _, raw := range paths {
		rel := strings.TrimPrefix(filepath.ToSlash(raw), "./")
		kind := kindOf(rel)
		if kind == "" {
			continue
		}
		at := index[kind]
		if len(groups[at].paths) >= MaxArtifacts {
			continue
		}
		groups[at].paths = append(groups[at].paths, rel)
	}
	return groups
}

// kindOf names the artifact class of a path, or "" when this package cannot validate it.
func kindOf(rel string) string {
	lower := strings.ToLower(rel)
	base := path.Base(lower)

	switch {
	case strings.HasPrefix(base, "docker-compose.") || strings.HasPrefix(base, "compose."):
		if strings.HasSuffix(base, ".yml") || strings.HasSuffix(base, ".yaml") {
			return KindCompose
		}
	case base == "chart.yaml":
		// helm lint takes the CHART DIRECTORY, and the presence of Chart.yaml is what identifies one.
		return KindHelm
	}

	if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
		return ""
	}
	for _, dir := range []string{"k8s/", "kubernetes/", "manifests/", "deploy/", "deployment/"} {
		if strings.Contains(lower, dir) {
			return KindK8s
		}
	}
	// A workflow has a schema this repository already compiles, and nothing else was checking it.
	if strings.Contains(lower, ".github/workflows/") {
		return KindYAML
	}
	return ""
}

func validateOne(
	ctx context.Context, runner *validator.Runner, kind, rel, abs string,
) scanner.ScanValidation {
	// A PATH THAT IS NOT THERE IS NOT AN INVALID ARTIFACT. Several of these tools exit non-zero with no
	// parseable finding when handed a path that does not exist, which arrives here as `failed` — telling a
	// user their compose file is broken when the truth is that it was not found. The scan supplies paths it
	// just walked, so this should not happen; when it does, the cause is a classifier or a race with a
	// delete, and neither is a statement about the file's contents.
	if _, err := os.Stat(abs); err != nil {
		return scanner.ScanValidation{
			Path:   rel,
			Kind:   kind,
			Status: StatusErrored,
			Detail: truncate("the path could not be read: " + err.Error()),
		}
	}

	bounded, cancel := context.WithTimeout(ctx, PerArtifactTimeout)
	defer cancel()

	var outcome validator.Outcome
	var err error
	switch kind {
	case KindCompose:
		outcome, err = runner.ValidateCompose(bounded, abs)
	case KindK8s:
		outcome, err = runner.ValidateK8s(bounded, abs)
	case KindHelm:
		// The chart DIRECTORY, not the Chart.yaml inside it.
		outcome, err = runner.ValidateHelm(bounded, filepath.Dir(abs))
	case KindYAML:
		outcome, err = runner.ValidateYAML(bounded, abs, validator.SchemaFor(rel))
	}

	entry := scanner.ScanValidation{
		Path:        rel,
		Kind:        kind,
		Tool:        outcome.Tool,
		ToolVersion: outcome.ToolVersion,
	}

	switch {
	case errors.Is(err, validator.ErrToolMissing):
		// NOT A PASS AND NOT A FAILURE. The artifact was not judged, and the report must say which tool
		// was absent so the user can install it rather than wonder why the check is quiet.
		entry.Status = StatusToolMissing
		entry.Detail = err.Error()
		return entry
	case err != nil:
		entry.Status = StatusErrored
		entry.Detail = truncate(err.Error())
		return entry
	}

	entry.FindingCount = len(outcome.Findings)
	for _, finding := range outcome.Findings {
		// CRITICAL and HIGH are the two that mean "this artifact is wrong", and they are what the score
		// acts on. MEDIUM and below are reported in the count but do not make an artifact failing: a
		// yamllint line-length warning is not a broken manifest, and treating it as one would make the
		// check something users switch off.
		if finding.Severity != validator.SeverityCritical && finding.Severity != validator.SeverityHigh {
			continue
		}
		entry.ErrorCount++
		if entry.Detail == "" {
			entry.Detail = truncate(finding.Message)
			entry.Line = finding.Line
		}
	}
	if outcome.Passed {
		entry.Status = StatusPassed
	} else {
		entry.Status = StatusFailed
		if entry.Detail == "" {
			// The tool failed without a parsed finding, so its own words are the only honest detail.
			entry.Detail = truncate(firstLine(outcome.Output))
		}
	}
	return entry
}

// DetailLimit bounds one detail string. The backend stores these and a tool can emit a great deal.
const DetailLimit = 500

func truncate(s string) string {
	s = strings.TrimSpace(s)
	if len(s) <= DetailLimit {
		return s
	}
	return s[:DetailLimit] + "\u2026"
}

func firstLine(s string) string {
	if index := strings.IndexAny(s, "\r\n"); index >= 0 {
		return s[:index]
	}
	return s
}
