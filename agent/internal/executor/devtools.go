// SPDX-License-Identifier: Apache-2.0

// `devtools.run`: the project's own tests, linters, build, local compose stack and migrations. §2.8.
//
// ONE OPERATION, AND THE ARGUMENT IS A KIND — NOT A COMMAND LINE. This is the file where an
// arbitrary-shell operation would most naturally appear, and it deliberately does not: the envelope names a
// kind from `devToolKinds`, and the agent chooses the argument vector. An envelope saying
// `{"kind":"tests"}` cannot become `rm -rf`, because no field in `devToolArgs` reaches a shell.
//
// MUTATING AND APPROVAL-REQUIRED, which deserves stating because "run the tests" sounds read-only. It is
// not: a project's own test command is code the project controls, a build writes artifacts, compose starts
// containers and a migration changes a database. Treating any of those as a read would give a panel the
// authority to execute the repository's own scripts without a human, which is a larger authority than
// anything else in this catalogue.
//
// The tool is resolved per ECOSYSTEM from what the workspace actually contains, so a Go project's "tests"
// is `go test` and a Node project's is the package manager's. A project where the kind cannot be resolved
// is refused BY NAME rather than run with a guess.
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

var (
	// ErrUnknownDevToolKind refuses a kind outside the closed set by name.
	ErrUnknownDevToolKind = errors.New("executor: that dev-tool kind is not in the closed set")
	// ErrNoEcosystem is returned when nothing in the workspace says how to run this kind. Named
	// separately from a failure: nothing was attempted, and the operator needs to add a manifest rather
	// than debug a tool.
	ErrNoEcosystem = errors.New("executor: no ecosystem in this workspace can run that")
)

// devToolKinds is the closed set. The value is documentation only; the vector is resolved below.
var devToolKinds = map[string]string{
	"tests":      "the project's own test suite",
	"lint":       "the project's own linters",
	"build":      "the project's own build",
	"compose":    "the local docker compose stack",
	"migrations": "the project's database migrations",
}

// devToolArgs is what the envelope carries. NO command, NO arguments, NO flags — a kind and a timeout.
type devToolArgs struct {
	Kind string `json:"kind"`
	// TimeoutSeconds bounds one run. Clamped; a test suite that runs longer than the operation's budget
	// would report "the operation timed out" rather than "the suite is slow".
	TimeoutSeconds int `json:"timeout_seconds,omitempty"`
}

// DevToolReport is what one run produces.
type DevToolReport struct {
	Kind string `json:"kind"`
	// Tool and Args are what was actually executed, reported so an operator can reproduce it by hand.
	Tool string   `json:"tool"`
	Args []string `json:"args"`
	// Ecosystem names how the vector was chosen (`go`, `node`, `python`, `compose`), so "why did it run
	// that" has an answer.
	Ecosystem string `json:"ecosystem"`
	Passed    bool   `json:"passed"`
	ExitCode  int    `json:"exit_code"`
	// Output is the tail; a test suite can print megabytes.
	Output    string `json:"output"`
	Truncated bool   `json:"truncated"`
	// DurationSeconds, because "the lint got slower" is a thing a panel should be able to show.
	DurationSeconds int    `json:"duration_seconds"`
	ObservedAt      string `json:"observed_at"`
}

const (
	// MaxDevToolOutput caps the reported output at roughly a screenful of scrollback.
	MaxDevToolOutput = 64 * 1024
	// DefaultDevToolTimeout is what a run gets when the envelope does not say.
	DefaultDevToolTimeout = 10 * time.Minute
	// MaxDevToolTimeout is the ceiling. Below `timeoutDevTools` on the dispatch row, so a slow suite is
	// reported as slow rather than as an operation that timed out.
	MaxDevToolTimeout = 25 * time.Minute
)

func devToolTimeout(seconds int) time.Duration {
	if seconds <= 0 {
		return DefaultDevToolTimeout
	}
	requested := time.Duration(seconds) * time.Second
	if requested > MaxDevToolTimeout {
		return MaxDevToolTimeout
	}
	return requested
}

// exists reports whether a workspace-relative file is present.
func exists(root, name string) bool {
	info, err := os.Stat(filepath.Join(root, name))
	return err == nil && !info.IsDir()
}

// resolveDevTool chooses the tool and vector for a kind from what the workspace contains.
//
// Ordered, not map-ranged: a repository with both `go.mod` and `package.json` must resolve the same way on
// every run, or two runs of "tests" would test different things.
func resolveDevTool(root, kind string) (tool string, args []string, ecosystem string, err error) {
	switch kind {
	case "tests":
		switch {
		case exists(root, "go.mod"):
			return "go", []string{"test", "./..."}, "go", nil
		case exists(root, "package.json"):
			return "npm", []string{"test", "--silent"}, "node", nil
		case exists(root, "pyproject.toml"), exists(root, "pytest.ini"):
			return "pytest", []string{"-q"}, "python", nil
		}
	case "lint":
		switch {
		case exists(root, "go.mod"):
			return "go", []string{"vet", "./..."}, "go", nil
		case exists(root, "package.json"):
			return "npm", []string{"run", "lint", "--silent"}, "node", nil
		case exists(root, "pyproject.toml"):
			return "ruff", []string{"check", "."}, "python", nil
		}
	case "build":
		switch {
		case exists(root, "go.mod"):
			return "go", []string{"build", "./..."}, "go", nil
		case exists(root, "package.json"):
			return "npm", []string{"run", "build", "--silent"}, "node", nil
		case exists(root, "Dockerfile"):
			// `--load` is absent on purpose: building an image is a build, pushing it is not, and this
			// operation does neither to a registry.
			return "docker", []string{"build", "."}, "docker", nil
		}
	case "compose":
		switch {
		case exists(root, "docker-compose.yml"), exists(root, "compose.yml"),
			exists(root, "docker-compose.yaml"), exists(root, "compose.yaml"):
			// `up -d --wait`, never `down -v`: this operation starts a local stack and cannot remove a
			// volume. A destructive compose verb would be a different authority.
			return "docker", []string{"compose", "up", "-d", "--wait"}, "compose", nil
		}
	case "migrations":
		switch {
		case exists(root, "alembic.ini"):
			return "alembic", []string{"upgrade", "head"}, "python", nil
		case exists(root, "package.json") && exists(root, "prisma/schema.prisma"):
			return "npx", []string{"prisma", "migrate", "deploy"}, "node", nil
		}
	default:
		return "", nil, "", fmt.Errorf("%w: %q", ErrUnknownDevToolKind, kind)
	}
	return "", nil, "", fmt.Errorf(
		"%w: %q — nothing in this workspace declares how to %s", ErrNoEcosystem, kind, kind)
}

func tailOutput(output string) (string, bool) {
	if len(output) <= MaxDevToolOutput {
		return output, false
	}
	return output[len(output)-MaxDevToolOutput:], true
}

// devToolsRun is the handler.
func devToolsRun(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args devToolArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: dev-tool arguments: %v", ErrBadArgs, err)
	}
	kind := strings.TrimSpace(args.Kind)
	if _, known := devToolKinds[kind]; !known {
		return Result{}, fmt.Errorf("%w: %q", ErrUnknownDevToolKind, kind)
	}

	tool, vector, ecosystem, err := resolveDevTool(d.root, kind)
	if err != nil {
		return Result{}, err
	}

	runner := &validator.Runner{Dir: d.root}
	if _, lookErr := runner.Look(tool); lookErr != nil {
		return Result{}, fmt.Errorf("executor: %s is not on PATH, so %s cannot run: %w", tool, kind, lookErr)
	}

	budget := devToolTimeout(args.TimeoutSeconds)
	sink.Progress(20, string(OpDevToolsRun), fmt.Sprintf("%s: %s %s", kind, tool, strings.Join(vector, " ")))

	runCtx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	started := time.Now()
	outcome, runErr := runner.Run(runCtx, tool, vector...)
	elapsed := int(time.Since(started).Seconds())

	output, truncated := tailOutput(outcome.Output)
	report := DevToolReport{
		Kind: kind, Tool: tool, Args: vector, Ecosystem: ecosystem,
		// A FAILING SUITE IS A RESULT, NOT AN ERROR. `go test` exiting 1 means the tests failed, which is
		// exactly what the operator asked to find out; returning an error would make a red suite
		// indistinguishable from an agent that could not run it.
		Passed: runErr == nil && outcome.Passed,
		Output: output, Truncated: truncated,
		DurationSeconds: elapsed, ObservedAt: d.now().UTC().Format(time.RFC3339),
	}
	if !report.Passed {
		report.ExitCode = 1
	}

	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable dev-tool report: %w", err)
	}
	status := "passed"
	if !report.Passed {
		status = "failed"
	}
	sink.Progress(100, string(OpDevToolsRun), fmt.Sprintf("%s %s in %ds", kind, status, elapsed))
	return Result{Status: status, Output: string(encoded)}, nil
}
