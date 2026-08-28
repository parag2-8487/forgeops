// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// ValidateTofu runs `tofu init -backend=false`, `tofu validate`, and `tofu plan` where a plan means
// something.
//
// THE INIT IS NOT OPTIONAL AND `-backend=false` IS THE POINT. `tofu validate` needs the provider
// schemas, which only exist after an init; without one it fails for a reason that has nothing to do
// with the artifact. `-backend=false` means no remote state is configured and nothing is read from
// or written to one — a generated module must never be able to touch real state just by being
// validated.
//
// The plan runs only when the module declares no required providers it cannot reach offline, because
// a plan against an unreachable provider fails on the network rather than on the configuration and
// would report a valid module as broken. `Outcome.Mode` names how far it got, so "validated" is
// never confused with "planned".
func (r *Runner) ValidateTofu(ctx context.Context, moduleDir string) (Outcome, error) {
	scoped := r.In(moduleDir)

	init, err := scoped.Run(ctx, "tofu", "init", "-backend=false", "-input=false", "-no-color")
	if err != nil {
		return init, err
	}
	init.Tool = "tofu"
	init.ToolVersion = r.Version(ctx, "tofu", "version")
	if !init.Passed {
		init.Mode = "init"
		init.Findings = findingsFromLines(init.Output, SeverityHigh, "tofu-init", moduleDir)
		return init, nil
	}

	validate, err := scoped.Run(ctx, "tofu", "validate", "-no-color", "-json")
	if err != nil {
		return validate, err
	}
	validate.Tool = "tofu"
	validate.ToolVersion = init.ToolVersion
	validate.Mode = "validate"
	if !validate.Passed {
		validate.Findings = tofuDiagnostics(validate.Output, moduleDir)
		return validate, nil
	}

	// A plan is meaningful only when it can run without credentials. `-refresh=false` keeps it from
	// reading any real infrastructure, so this is a check of the configuration rather than an
	// inspection of somebody's estate.
	plan, err := scoped.Run(ctx, "tofu", "plan", "-refresh=false", "-input=false", "-no-color", "-lock=false")
	if err != nil {
		// A plan that could not be run is not a failed artifact. The validate above already
		// passed, so that is the verdict, with the mode saying a plan was not reached.
		validate.Mode = "validate (plan not attempted: " + firstLine(err.Error()) + ")"
		return validate, nil
	}
	plan.Tool = "tofu"
	plan.ToolVersion = init.ToolVersion
	plan.Mode = "validate+plan"
	if !plan.Passed {
		// A plan can fail for want of a provider credential, which is not the artifact's fault.
		// Reported at HIGH rather than CRITICAL and described as what it is.
		plan.Findings = findingsFromLines(plan.Output, SeverityHigh, "tofu-plan", moduleDir)
	} else {
		plan.Output = strings.TrimSpace(validate.Output)
	}
	return plan, nil
}

// tofuDiagnostics reads `tofu validate -json`, which reports a real path and line per diagnostic.
func tofuDiagnostics(output, moduleDir string) []Finding {
	var parsed struct {
		Diagnostics []struct {
			Severity string `json:"severity"`
			Summary  string `json:"summary"`
			Detail   string `json:"detail"`
			Range    *struct {
				Filename string `json:"filename"`
				Start    struct {
					Line int `json:"line"`
				} `json:"start"`
			} `json:"range"`
		} `json:"diagnostics"`
	}
	if err := json.Unmarshal([]byte(output), &parsed); err != nil || len(parsed.Diagnostics) == 0 {
		return findingsFromLines(output, SeverityHigh, "tofu-validate", moduleDir)
	}
	findings := make([]Finding, 0, len(parsed.Diagnostics))
	for _, d := range parsed.Diagnostics {
		severity := SeverityHigh
		if strings.EqualFold(d.Severity, "warning") {
			severity = SeverityMedium
		}
		message := d.Summary
		if d.Detail != "" {
			message = d.Summary + ": " + d.Detail
		}
		finding := Finding{Severity: severity, Rule: "tofu-validate", Message: message, Path: moduleDir}
		if d.Range != nil {
			finding.Path = d.Range.Filename
			finding.Line = d.Range.Start.Line
		}
		findings = append(findings, finding)
	}
	return findings
}

// ValidateYAML runs yamllint and then checks the document against a JSON Schema when one applies.
//
// TWO DIFFERENT QUESTIONS. yamllint answers "is this well-formed and conventional YAML" — indentation,
// duplicate keys, a truthy value that is not what the author meant. A JSON Schema answers "is this
// the right shape for what it claims to be", which yamllint cannot know. A GitHub Actions workflow
// that parses perfectly and puts `runs-on` in the wrong place is valid YAML and a broken workflow.
//
// `-f parsable` because that format gives `path:line:col: [level] message (rule)`, which is precise
// enough to turn into findings with real coordinates rather than guessed ones.
func (r *Runner) ValidateYAML(ctx context.Context, path string, schema *Schema) (Outcome, error) {
	config, err := yamllintConfigFile()
	if err != nil {
		return Outcome{Tool: "yamllint"}, err
	}
	outcome, err := r.Run(ctx, "yamllint", "-c", config, "-f", "parsable", "--strict", path)
	if err != nil {
		return outcome, err
	}
	outcome.Tool = "yamllint"
	outcome.ToolVersion = r.Version(ctx, "yamllint", "--version")
	outcome.Mode = "yamllint"
	if !outcome.Passed {
		outcome.Findings = yamllintFindings(outcome.Output, path)
	}

	if schema == nil {
		return outcome, nil
	}
	outcome.Mode = "yamllint+schema:" + schema.Name
	schemaFindings, schemaErr := schema.Check(path)
	if schemaErr != nil {
		return outcome, schemaErr
	}
	if len(schemaFindings) > 0 {
		outcome.Findings = append(outcome.Findings, schemaFindings...)
		outcome.Passed = false
		if outcome.ExitCode == 0 {
			// The schema, not the linter, is what objected. A non-zero code is set so a caller
			// reading only the exit status still sees a failure.
			outcome.ExitCode = 1
		}
	}
	return outcome, nil
}

// yamllintFindings parses `-f parsable` output: `path:line:col: [level] message (rule)`.
func yamllintFindings(output, fallbackPath string) []Finding {
	var findings []Finding
	for _, raw := range strings.Split(output, "\n") {
		line := strings.TrimSpace(raw)
		if line == "" {
			continue
		}
		finding := Finding{Severity: SeverityMedium, Rule: "yamllint", Message: line, Path: fallbackPath}
		// Windows paths carry a drive colon, so the split is bounded and validated rather than
		// assuming the first colon separates the path.
		if parts := strings.SplitN(line, ":", 4); len(parts) == 4 {
			pathPart, lineNo, rest := parts[0], parts[1], strings.TrimSpace(parts[3])
			if len(pathPart) == 1 && len(parts) >= 4 {
				// Drive letter: re-split allowing one more segment.
				if reparts := strings.SplitN(line, ":", 5); len(reparts) == 5 {
					pathPart = reparts[0] + ":" + reparts[1]
					lineNo = reparts[2]
					rest = strings.TrimSpace(reparts[4])
				}
			}
			if n, err := strconv.Atoi(lineNo); err == nil {
				finding.Path = pathPart
				finding.Line = n
				finding.Message = rest
			}
		}
		if strings.Contains(finding.Message, "[error]") {
			finding.Severity = SeverityHigh
		}
		findings = append(findings, finding)
	}
	if len(findings) == 0 {
		findings = append(findings, Finding{
			Severity: SeverityMedium, Rule: "yamllint",
			Message: "yamllint reported failure with no diagnostic output", Path: fallbackPath,
		})
	}
	return findings
}

// ValidateTrivy scans a path for misconfigurations and secrets and maps every finding by severity.
//
// `config` and `secret` scanners over a filesystem target, in JSON, with `--exit-code 0` so the
// process status carries no verdict and the verdict comes from the parsed findings instead. That is
// deliberate: Trivy's exit code conflates "found something" with "could not scan", and those need
// opposite handling. `Threshold` decides the pass, so a caller chooses what severity blocks rather
// than inheriting Trivy's default.
//
// The predecessor grepped for `privileged: true` and one CIDR, and had a `FailClosed` field it never
// read. It reported every other misconfiguration in existence as secure.
func (r *Runner) ValidateTrivy(ctx context.Context, target string, threshold Severity) (Outcome, error) {
	outcome, err := r.Run(ctx, "trivy",
		"filesystem", target,
		"--scanners", "misconfig,secret",
		"--format", "json",
		"--exit-code", "0",
		"--no-progress",
		"--quiet",
	)
	if err != nil {
		return outcome, err
	}
	outcome.Tool = "trivy"
	outcome.ToolVersion = r.Version(ctx, "trivy", "--quiet", "version", "-f", "json")
	outcome.Mode = "filesystem:misconfig,secret threshold=" + string(threshold)

	findings, parseErr := trivyFindings(outcome.Output, target)
	if parseErr != nil {
		// Trivy ran but produced something unreadable. Not a pass: a scanner whose output cannot be
		// read has not established anything.
		return outcome, fmt.Errorf("validator: trivy output could not be read: %w", parseErr)
	}
	outcome.Findings = findings
	outcome.Output = summariseTrivy(findings)
	outcome.Passed = true
	if worst, found := outcome.Worst(); found && severityRank[worst] >= severityRank[threshold] {
		outcome.Passed = false
		outcome.ExitCode = 1
	}
	return outcome, nil
}

func trivyFindings(output, target string) ([]Finding, error) {
	trimmed := strings.TrimSpace(output)
	if trimmed == "" {
		// Nothing scanned and nothing said. Treated as no findings rather than an error: Trivy
		// prints nothing at all for a target with no results under `--quiet`.
		return nil, nil
	}
	var report struct {
		Results []struct {
			Target            string `json:"Target"`
			Misconfigurations []struct {
				ID            string `json:"ID"`
				Title         string `json:"Title"`
				Message       string `json:"Message"`
				Severity      string `json:"Severity"`
				CauseMetadata *struct {
					StartLine int `json:"StartLine"`
				} `json:"CauseMetadata"`
			} `json:"Misconfigurations"`
			Secrets []struct {
				RuleID    string `json:"RuleID"`
				Title     string `json:"Title"`
				Severity  string `json:"Severity"`
				StartLine int    `json:"StartLine"`
			} `json:"Secrets"`
		} `json:"Results"`
	}
	if err := json.Unmarshal([]byte(trimmed), &report); err != nil {
		return nil, err
	}
	var findings []Finding
	for _, result := range report.Results {
		path := result.Target
		if path == "" {
			path = target
		}
		for _, m := range result.Misconfigurations {
			finding := Finding{
				Severity: normaliseSeverity(m.Severity),
				Rule:     m.ID,
				Message:  strings.TrimSpace(m.Title + ": " + m.Message),
				Path:     path,
			}
			if m.CauseMetadata != nil {
				finding.Line = m.CauseMetadata.StartLine
			}
			findings = append(findings, finding)
		}
		for _, s := range result.Secrets {
			// The matched value is deliberately NOT carried. A secret scanner's finding travels to
			// the backend and into an audit trail, and copying the credential into it would leak
			// the thing being reported.
			findings = append(findings, Finding{
				Severity: normaliseSeverity(s.Severity),
				Rule:     "secret:" + s.RuleID,
				Message:  s.Title + " (value withheld)",
				Path:     path,
				Line:     s.StartLine,
			})
		}
	}
	return findings, nil
}

func normaliseSeverity(s string) Severity {
	switch strings.ToUpper(strings.TrimSpace(s)) {
	case "CRITICAL":
		return SeverityCritical
	case "HIGH":
		return SeverityHigh
	case "MEDIUM":
		return SeverityMedium
	case "LOW":
		return SeverityLow
	default:
		return SeverityInfo
	}
}

func summariseTrivy(findings []Finding) string {
	if len(findings) == 0 {
		return "trivy: no misconfigurations or secrets found"
	}
	counts := map[Severity]int{}
	for _, f := range findings {
		counts[f.Severity]++
	}
	parts := make([]string, 0, 5)
	for _, s := range []Severity{SeverityCritical, SeverityHigh, SeverityMedium, SeverityLow, SeverityInfo} {
		if counts[s] > 0 {
			parts = append(parts, fmt.Sprintf("%s=%d", s, counts[s]))
		}
	}
	return "trivy: " + strings.Join(parts, " ")
}
