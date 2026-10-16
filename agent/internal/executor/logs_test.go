// SPDX-License-Identifier: Apache-2.0

// The log reads' bounds and refusals.
//
// The property that matters most is the TRUNCATION MARK: a tail that is not reported as a tail lets a
// reader conclude an error never happened when it fell off the top. The bounds themselves matter because an
// unbounded read of a month-old container exhausts the agent and then the browser.
package executor

import (
	"context"
	"strings"
	"testing"
)

func TestLogBoundsAreClampedInBothDirections(t *testing.T) {
	tail, since := clampLogBounds(0, 0)
	if tail != DefaultLogLines {
		t.Errorf("an unspecified tail resolved to %d, want the default %d", tail, DefaultLogLines)
	}
	if since != 0 {
		t.Errorf("an unspecified window resolved to %d, want 0 (no window)", since)
	}

	// Above the cap is clamped DOWN rather than refused: a panel asking for too much should get the most
	// it can have, not an error.
	if tail, _ = clampLogBounds(MaxLogLines*10, 0); tail != MaxLogLines {
		t.Errorf("a huge tail resolved to %d, want %d", tail, MaxLogLines)
	}
	if _, since = clampLogBounds(0, MaxLogSinceSeconds*10); since != MaxLogSinceSeconds {
		t.Errorf("a huge window resolved to %d, want %d", since, MaxLogSinceSeconds)
	}
	// Negative is not a window.
	if _, since = clampLogBounds(0, -60); since != 0 {
		t.Errorf("a negative window resolved to %d, want 0", since)
	}
	// A value inside the caps survives, or the bounds would be a fixed size rather than a limit.
	if tail, since = clampLogBounds(50, 300); tail != 50 || since != 300 {
		t.Errorf("an in-range request resolved to (%d, %d), want (50, 300)", tail, since)
	}
}

func TestSplitLinesDropsBlanksAndSurvivesCRLF(t *testing.T) {
	lines := splitLines("first\r\nsecond\n\n  \nthird\n")
	if len(lines) != 3 {
		t.Fatalf("split to %d line(s): %v", len(lines), lines)
	}
	// The CR must not survive into the payload, or every line in a browser gains a stray character.
	for _, line := range lines {
		if strings.Contains(line, "\r") {
			t.Errorf("a carriage return survived into %q", line)
		}
	}
	if lines[0] != "first" || lines[2] != "third" {
		t.Errorf("split to %v", lines)
	}
}

func TestContainerLogsRefuseAnEmptyTarget(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	_, err = d.Execute(context.Background(),
		verified(t, OpDockerContainerLogs, "", map[string]any{"container": ""}, 101), nil)
	if err == nil {
		t.Fatal("a log read naming no container was accepted")
	}
}

func TestPodDetailNeedsBothNamespaceAndPod(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for index, args := range []map[string]any{
		{"namespace": "default", "pod": ""},
		{"namespace": "", "pod": "api-abc"},
	} {
		if _, err := d.Execute(context.Background(),
			verified(t, OpKubernetesPodDetail, "", args, int64(102+index)), nil); err == nil {
			t.Errorf("%v was accepted; an unqualified pod name is not one pod", args)
		}
	}
}

func TestTheLogReadsAreApprovalFreeAndNonMutating(t *testing.T) {
	// Same pairing as the inventories: a log panel refreshes, and these cannot change anything.
	for _, op := range []Operation{OpDockerContainerLogs, OpKubernetesPodDetail} {
		row := handlerTable[op]
		if row.mutating {
			t.Errorf("%q is mutating, and reading output changes nothing", op)
		}
		if row.requiresApproval {
			t.Errorf("%q requires an approval, so a log panel could not refresh", op)
		}
		if !row.implemented {
			t.Errorf("%q is catalogued with no body", op)
		}
	}
}
