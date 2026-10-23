// SPDX-License-Identifier: Apache-2.0

// `devtools.run`: the closed kind set, the per-ecosystem resolution, and the two refusals.
//
// The property that matters most is the ABSENCE of a command line. This is the operation where an
// arbitrary-shell escape would most naturally appear, so the tests assert that no field in the envelope
// reaches the vector and that the destructive verbs are unreachable.
package executor

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeFile(t *testing.T, root, name string) {
	t.Helper()
	path := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte("{}"), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

func TestDevToolKindsAreClosedAndComplete(t *testing.T) {
	// §2.8's five boxes, one kind each. A missing kind is a panel button that cannot work.
	for _, kind := range []string{"tests", "lint", "build", "compose", "migrations"} {
		if _, ok := devToolKinds[kind]; !ok {
			t.Errorf("%q is not in the closed set", kind)
		}
	}
	if len(devToolKinds) != 5 {
		t.Errorf("the kind set has %d entries, expected 5; a new one is new execution authority",
			len(devToolKinds))
	}
	// The kinds that must NOT exist. Each would be an arbitrary-execution escape wearing a kind's name.
	for _, forbidden := range []string{"exec", "shell", "run", "custom", "script", "any"} {
		if _, ok := devToolKinds[forbidden]; ok {
			t.Errorf("%q is a kind, which is an arbitrary-execution escape", forbidden)
		}
	}
}

func TestDevToolResolutionIsPerEcosystemAndDeterministic(t *testing.T) {
	cases := []struct {
		name      string
		files     []string
		kind      string
		wantTool  string
		wantFirst string
		ecosystem string
	}{
		{"go tests", []string{"go.mod"}, "tests", "go", "test", "go"},
		{"node tests", []string{"package.json"}, "tests", "npm", "test", "node"},
		{"python tests", []string{"pyproject.toml"}, "tests", "pytest", "-q", "python"},
		{"go lint", []string{"go.mod"}, "lint", "go", "vet", "go"},
		{"node lint", []string{"package.json"}, "lint", "npm", "run", "node"},
		{"python lint", []string{"pyproject.toml"}, "lint", "ruff", "check", "python"},
		{"go build", []string{"go.mod"}, "build", "go", "build", "go"},
		{"docker build", []string{"Dockerfile"}, "build", "docker", "build", "docker"},
		{"compose", []string{"docker-compose.yml"}, "compose", "docker", "compose", "compose"},
		{"alembic migrations", []string{"alembic.ini"}, "migrations", "alembic", "upgrade", "python"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			for _, file := range tc.files {
				writeFile(t, root, file)
			}
			tool, args, ecosystem, err := resolveDevTool(root, tc.kind)
			if err != nil {
				t.Fatalf("resolving %s: %v", tc.kind, err)
			}
			if tool != tc.wantTool || ecosystem != tc.ecosystem {
				t.Errorf("resolved to %s/%s, want %s/%s", tool, ecosystem, tc.wantTool, tc.ecosystem)
			}
			if len(args) == 0 || args[0] != tc.wantFirst {
				t.Errorf("vector is %v, want it to start with %q", args, tc.wantFirst)
			}
		})
	}
}

func TestGoWinsOverNodeSoTwoRunsTestTheSameThing(t *testing.T) {
	// A repository with both manifests must resolve identically every time. Map iteration order would
	// make two runs of "tests" test different things, which is worse than either choice.
	root := t.TempDir()
	writeFile(t, root, "go.mod")
	writeFile(t, root, "package.json")
	for attempt := 0; attempt < 5; attempt++ {
		tool, _, ecosystem, err := resolveDevTool(root, "tests")
		if err != nil {
			t.Fatalf("resolving: %v", err)
		}
		if tool != "go" || ecosystem != "go" {
			t.Fatalf("attempt %d resolved to %s/%s", attempt, tool, ecosystem)
		}
	}
}

func TestNoDestructiveVerbIsReachable(t *testing.T) {
	// The vectors are fixed, so this enumerates them and checks what they can express. `compose down -v`
	// removes volumes and `docker system prune` removes everything unused; neither is reachable.
	root := t.TempDir()
	for _, file := range []string{"go.mod", "package.json", "pyproject.toml", "Dockerfile",
		"docker-compose.yml", "alembic.ini"} {
		writeFile(t, root, file)
	}
	forbidden := []string{"down", "rm", "prune", "-v", "--volumes", "push", "delete", "reset"}
	for kind := range devToolKinds {
		_, args, _, err := resolveDevTool(root, kind)
		if err != nil {
			t.Fatalf("resolving %s: %v", kind, err)
		}
		for _, arg := range args {
			for _, bad := range forbidden {
				if arg == bad {
					t.Errorf("the %q vector carries %q: %v", kind, bad, args)
				}
			}
		}
	}
}

func TestAnUnknownKindAndAnUnresolvableWorkspaceAreDifferentRefusals(t *testing.T) {
	root := t.TempDir()
	// An unknown kind: the caller asked for something that does not exist.
	if _, _, _, err := resolveDevTool(root, "deploy"); err == nil {
		t.Error("an unknown kind resolved")
	} else if !strings.Contains(err.Error(), "closed set") {
		t.Errorf("an unknown kind was refused for the wrong reason: %v", err)
	}
	// An empty workspace: the kind is real and nothing here declares how to do it. A different remedy —
	// add a manifest — so a different message.
	if _, _, _, err := resolveDevTool(root, "tests"); err == nil {
		t.Error("tests resolved in an empty workspace")
	} else if !strings.Contains(err.Error(), "no ecosystem") {
		t.Errorf("an unresolvable workspace was refused for the wrong reason: %v", err)
	}
}

func TestDevToolTimeoutIsClampedBelowTheOperationBudget(t *testing.T) {
	if devToolTimeout(0) != DefaultDevToolTimeout {
		t.Errorf("an unspecified budget resolved to %v", devToolTimeout(0))
	}
	if devToolTimeout(100*60*60) != MaxDevToolTimeout {
		t.Errorf("a huge budget resolved to %v, want %v", devToolTimeout(100*60*60), MaxDevToolTimeout)
	}
	// THE CLAMP MUST STAY BELOW THE DISPATCH ROW'S BUDGET, or a slow suite surfaces as "the operation
	// timed out" instead of "the suite is slow" — two different facts with two different remedies. This is
	// the same defect the deployment operation's first version had.
	if MaxDevToolTimeout >= handlerTable[OpDevToolsRun].timeout {
		t.Errorf("the per-run clamp %v is not below the operation budget %v",
			MaxDevToolTimeout, handlerTable[OpDevToolsRun].timeout)
	}
	if devToolTimeout(90) != 90*time.Second {
		t.Errorf("an in-range budget was altered: %v", devToolTimeout(90))
	}
}

func TestOutputIsTailedAndTheTailIsMarked(t *testing.T) {
	short, truncated := tailOutput("all good")
	if short != "all good" || truncated {
		t.Errorf("short output was altered: %q truncated=%v", short, truncated)
	}
	long := strings.Repeat("x", MaxDevToolOutput+100)
	tail, marked := tailOutput(long)
	if len(tail) != MaxDevToolOutput {
		t.Errorf("the tail is %d bytes, want %d", len(tail), MaxDevToolOutput)
	}
	// Marked, for the same reason a log tail is: unmarked truncation lets a reader conclude a failure
	// message was never printed.
	if !marked {
		t.Error("a truncated output was not marked as truncated")
	}
}

func TestAnUnknownKindIsRefusedThroughTheDispatcher(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for index, kind := range []string{"", "exec", "shell", "rm -rf /"} {
		_, err := d.Execute(context.Background(),
			verified(t, OpDevToolsRun, "approval-1", map[string]any{"kind": kind}, int64(151+index)), nil)
		if err == nil {
			t.Errorf("the kind %q was accepted", kind)
		}
	}
}
