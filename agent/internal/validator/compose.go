// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
)

// ValidateCompose runs `docker compose config` over one compose file.
//
// `config` rather than `up --dry-run`: it resolves the file, interpolates variables, merges
// overrides and validates the schema, and it does all of that without contacting a daemon or
// starting anything. That is exactly the assurance FR-27 wants before a user is shown the file, and
// it is safe to run against an artifact that arrived from a model.
//
// `--quiet` so a valid file produces no output at all; the interesting case is the error text.
func (r *Runner) ValidateCompose(ctx context.Context, path string) (Outcome, error) {
	outcome, err := r.Run(ctx, "docker", "compose", "-f", path, "config", "--quiet")
	if err != nil {
		return outcome, err
	}
	outcome.Tool = "docker compose"
	outcome.ToolVersion = r.Version(ctx, "docker", "compose", "version", "--short")
	outcome.Mode = "config"
	if !outcome.Passed {
		outcome.Findings = findingsFromLines(outcome.Output, SeverityHigh, "compose-config", path)
	}
	return outcome, nil
}

// ValidateK8s dry-runs a manifest, server-side when a cluster is reachable and client-side when not.
//
// THE TWO MODES ARE DIFFERENT ASSURANCES AND THE RESULT SAYS WHICH RAN, IN WORDS. A server dry run
// sends the object to the real API server, so admission webhooks, CRDs and the cluster's actual
// schema all apply, and `--validate=strict` rejects an unknown field. A client dry run has no schema
// source at all: modern kubectl fetches OpenAPI from the server, so with no cluster reachable
// `--validate=strict` fails trying to download it — which is a network error dressed as a validation
// failure, and would report a perfectly good manifest as broken.
//
// So the client path validates what can be validated locally — that the document parses, that every
// object has an apiVersion and a kind, and that kubectl can decode it into a typed object for the
// kinds it knows — and `Outcome.Mode` says in as many words that schema validation did not run.
// Reporting both paths as "validated" would let a manifest a cluster will reject reach a user as
// verified, and that is the failure this validator was rebuilt to stop.
//
// The predecessor took a `ClusterAvailable bool` from its caller and, when true, `return nil` without
// contacting anything. Reachability is now measured here rather than asserted by whoever constructed
// the validator.
func (r *Runner) ValidateK8s(ctx context.Context, path string) (Outcome, error) {
	if r.clusterReachable(ctx) {
		outcome, err := r.Run(ctx, "kubectl", "apply", "--dry-run=server", "--validate=strict", "-f", path)
		if err != nil {
			return outcome, err
		}
		outcome.Tool = "kubectl"
		outcome.ToolVersion = r.Version(ctx, "kubectl", "version", "--client=true", "-o", "yaml")
		outcome.Mode = "server-dry-run (admission and cluster schema applied)"
		if !outcome.Passed {
			outcome.Findings = findingsFromLines(outcome.Output, SeverityHigh, "kubectl-server-dry-run", path)
		}
		return outcome, nil
	}

	// No cluster. `kubectl apply --dry-run=client` cannot help here: it builds a RESTMapper and so
	// performs API discovery before reading the file, failing with "couldn't get current server API
	// group list" — a network error that says nothing about the manifest. `--validate=false` does not
	// avoid it either. So the manifest is checked locally against the kind-independent structure every
	// Kubernetes object must have, and the mode says what did not happen.
	outcome := Outcome{
		Tool:        "forgeops built-in schema",
		ToolVersion: "k8s-manifest",
		Command:     []string{"schema:k8s-manifest", path},
		Mode:        "local-schema (no cluster reachable, so kind-specific schema validation and admission did NOT run)",
		Passed:      true,
	}
	findings, err := checkAllDocuments(path, k8sManifestSchema)
	if err != nil {
		return outcome, err
	}
	if len(findings) > 0 {
		outcome.Passed = false
		outcome.ExitCode = 1
		outcome.Findings = findings
		outcome.Output = fmt.Sprintf("%d structural problem(s) in %s", len(findings), path)
	} else {
		outcome.Output = "every document declares apiVersion, kind and metadata.name"
	}
	return outcome, nil
}

// clusterReachable asks the cluster, rather than trusting a caller's flag.
//
// `kubectl version` with a short timeout: it contacts the API server, so a zero exit means there is
// something to dry-run against. A missing kubeconfig, an expired credential and an unreachable
// endpoint all land here as "not reachable", which is the right answer — all three mean a server
// dry run cannot be performed, whatever the reason.
func (r *Runner) clusterReachable(ctx context.Context) bool {
	probe, err := r.Run(ctx, "kubectl", "version", "-o", "json", "--request-timeout=5s")
	if err != nil || !probe.Passed {
		return false
	}
	// A client-only response still exits zero on some versions, so the server half is checked
	// explicitly rather than inferred from the exit code.
	var parsed struct {
		ServerVersion *struct {
			GitVersion string `json:"gitVersion"`
		} `json:"serverVersion"`
	}
	if json.Unmarshal([]byte(probe.Output), &parsed) != nil {
		return false
	}
	return parsed.ServerVersion != nil && parsed.ServerVersion.GitVersion != ""
}

// ValidateHelm runs `helm lint` and then `helm template --validate` over a chart directory.
//
// BOTH, BECAUSE THEY CATCH DIFFERENT THINGS. `lint` reads the chart's structure and conventions —
// a missing `Chart.yaml` field, a values file that does not parse. `template` actually renders the
// templates, which is what catches a Go-template error, a missing value referenced by a manifest,
// or output that is not valid Kubernetes YAML. A chart can lint cleanly and fail to render, and a
// user shown a chart that cannot render has been told something false.
//
// `--validate` on the template step sends the rendered objects to the API server for schema
// checking when one is reachable; without a cluster it degrades to local checking, and the mode
// reported says which happened.
func (r *Runner) ValidateHelm(ctx context.Context, chartDir string) (Outcome, error) {
	lint, err := r.Run(ctx, "helm", "lint", chartDir)
	if err != nil {
		return lint, err
	}
	lint.Tool = "helm"
	lint.ToolVersion = r.Version(ctx, "helm", "version", "--short")
	lint.Mode = "lint"
	if !lint.Passed {
		lint.Findings = findingsFromLines(lint.Output, SeverityHigh, "helm-lint", chartDir)
		return lint, nil
	}

	args := []string{"template", filepath.Base(chartDir), chartDir}
	mode := "lint+template"
	if r.clusterReachable(ctx) {
		args = append(args, "--validate")
		mode = "lint+template-validate"
	}
	tmpl, err := r.Run(ctx, "helm", args...)
	if err != nil {
		return tmpl, err
	}
	tmpl.Tool = "helm"
	tmpl.ToolVersion = lint.ToolVersion
	tmpl.Mode = mode
	// The rendered manifests are not the interesting part of a success, and they are large. Only
	// the failure text travels.
	if tmpl.Passed {
		tmpl.Output = strings.TrimSpace(lint.Output)
	} else {
		tmpl.Findings = findingsFromLines(tmpl.Output, SeverityHigh, "helm-template", chartDir)
	}
	return tmpl, nil
}

// findingsFromLines turns a tool's diagnostic text into findings without inventing precision.
//
// Every non-empty line that is not obvious noise becomes one finding at the given severity. No
// attempt is made to parse a location out of prose: where a tool reports a path and line in a form
// worth trusting, the specific validator parses it (see `ValidateYAML`). Guessing elsewhere would
// produce confident-looking coordinates that point at nothing.
func findingsFromLines(output string, severity Severity, rule string, path string) []Finding {
	var findings []Finding
	for _, raw := range strings.Split(output, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "[output truncated") {
			continue
		}
		findings = append(findings, Finding{
			Severity: severity,
			Rule:     rule,
			Message:  line,
			Path:     path,
		})
	}
	if len(findings) == 0 {
		// A non-zero exit with nothing to say still has to become a finding, or a caller
		// thresholding on findings would treat the failure as a pass.
		findings = append(findings, Finding{
			Severity: severity,
			Rule:     rule,
			Message:  fmt.Sprintf("%s reported failure with no diagnostic output", rule),
			Path:     path,
		})
	}
	return findings
}
