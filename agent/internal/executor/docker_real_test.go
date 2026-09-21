// SPDX-License-Identifier: Apache-2.0

// The Docker operations against a REAL daemon.
//
// Why this is separate from `docker_kubernetes_test.go` and why it exists at all: that file establishes
// the decisions, and a decision test cannot discover that `docker ps --format json` emits one object per
// line rather than an array, that `docker stats` blocks forever without `--no-stream`, or that
// `{{index .RepoDigests 0}}` prints `no value` for a locally built image. Each of those is a real property
// of a real tool, each would have produced a plausible-looking empty dashboard, and none is visible to a
// test that never runs it.
//
// Opt-in through `FORGEOPS_REAL_DOCKER`, like the keychain and cluster suites. A developer without Docker
// running is not a failing build; a CI runner that HAS Docker and does not use it is a missed check, which
// is why the opt-in is set in the workflow and asserted to be.
//
// SET IN TWO WORKFLOWS, and the second one is the one that matters. `Kubernetes & SPIRE CI` runs these
// against its own runner's daemon; `ci`'s `agent` job also sets it, on all three of its `go test`
// invocations, because that job enforces the coverage gate and produces the `-json` record that
// `check-no-skips.py` reads. Setting it on the test steps and not on the record was exactly the mistake
// that made the gate report ten undeclared capability skips: the suites had run twice with a daemon and
// the evidence said they never ran at all.
package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

const realDockerEnv = "FORGEOPS_REAL_DOCKER"

func requireRealDocker(t *testing.T) {
	t.Helper()
	if os.Getenv(realDockerEnv) != "1" {
		t.Skipf("set %s=1 to exercise the real Docker daemon; CI does this on the Linux runner, which "+
			"has one", realDockerEnv)
	}
	// Present-but-not-running is the commonest developer state and a FAILURE here, not a skip: the
	// opt-in is the statement that a daemon is supposed to be there.
	if _, err := exec.LookPath("docker"); err != nil {
		t.Fatalf("%s=1 but docker is not on PATH: %v", realDockerEnv, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if out, err := exec.CommandContext(ctx, "docker", "version", "--format", "{{.Server.Version}}").
		CombinedOutput(); err != nil {
		t.Fatalf("%s=1 but the daemon did not answer: %v — %s", realDockerEnv, err, out)
	}
}

// runDockerInventory drives the production path and decodes what the backend would receive.
func runDockerInventory(t *testing.T, stats bool, seq int64) DockerInventory {
	t.Helper()
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	var sawProgress bool
	result, err := d.Execute(ctx,
		verified(t, OpDockerInventory, "", map[string]any{"stats": stats}, seq),
		SinkFunc(func(int, string, string) { sawProgress = true }))
	if err != nil {
		t.Fatalf("docker.inventory against the real daemon: %v", err)
	}
	if !sawProgress {
		t.Error("the inventory reported no progress")
	}
	var inventory DockerInventory
	if err := json.Unmarshal([]byte(result.Output), &inventory); err != nil {
		t.Fatalf("decoding the inventory from %q: %v", result.Output, err)
	}
	return inventory
}

func TestRealDocker_InventoryReportsTheDaemonsOwnView(t *testing.T) {
	requireRealDocker(t)

	// A container of our own, so the assertions below are about something known to exist rather than
	// about whatever the host happened to be running.
	name := fmt.Sprintf("forgeops-inv-%d", time.Now().UnixNano()%1_000_000)
	create := exec.Command("docker", "run", "-d", "--name", name,
		"registry.k8s.io/pause:3.9")
	if out, err := create.CombinedOutput(); err != nil {
		t.Fatalf("starting a container to inventory: %v — %s", err, out)
	}
	t.Cleanup(func() {
		if out, err := exec.Command("docker", "rm", "-f", name).CombinedOutput(); err != nil {
			t.Logf("could not remove %s: %v — %s", name, err, out)
		}
	})

	inventory := runDockerInventory(t, false, 61)

	if inventory.DockerVersion == "" {
		t.Error("the inventory names no daemon version, so 'which daemon reported this' is unanswerable")
	}
	if inventory.ObservedAt == "" {
		t.Error("the inventory carries no observed_at, so a panel cannot tell current from stale")
	}
	if _, err := time.Parse(time.RFC3339, inventory.ObservedAt); err != nil {
		t.Errorf("observed_at %q is not RFC 3339: %v", inventory.ObservedAt, err)
	}

	// THE CONTAINER MUST BE THERE. An empty list from a host that is running something is the exact
	// failure a line-by-line decoder exists to prevent, and only a real daemon produces the output shape
	// that breaks the naive version.
	var found *DockerContainer
	for index := range inventory.Containers {
		if strings.Contains(inventory.Containers[index].Name, name) {
			found = &inventory.Containers[index]
		}
	}
	if found == nil {
		t.Fatalf("the container %s is running and the inventory listed %d container(s): %+v",
			name, len(inventory.Containers), inventory.Containers)
	}
	if found.State != "running" {
		t.Errorf("the container's state is %q, want running", found.State)
	}
	if found.Image == "" {
		t.Error("the container's image is empty")
	}

	// WITHOUT STATS, every measured figure must be nil — not zero. This is the assertion that keeps an
	// unmeasured container from rendering as an idle one.
	if found.CPUPercent != nil || found.MemoryBytes != nil {
		t.Errorf("stats were not requested but the figures are populated: cpu=%v mem=%v",
			found.CPUPercent, found.MemoryBytes)
	}
	if inventory.StatsSampled {
		t.Error("stats_sampled is true although no sample was requested")
	}

	// Images, volumes and networks: a real daemon always has at least one network (`bridge`), so an
	// empty list there means the decode failed rather than that the host is bare.
	if len(inventory.Networks) == 0 {
		t.Error("no networks were reported; every daemon has at least a bridge network")
	}
	if len(inventory.Images) == 0 {
		t.Error("no images were reported although a container is running from one")
	}
}

func TestRealDocker_StatsSampleIsMarkedAndPopulated(t *testing.T) {
	requireRealDocker(t)

	name := fmt.Sprintf("forgeops-stats-%d", time.Now().UnixNano()%1_000_000)
	if out, err := exec.Command("docker", "run", "-d", "--name", name,
		"registry.k8s.io/pause:3.9").CombinedOutput(); err != nil {
		t.Fatalf("starting a container to sample: %v — %s", err, out)
	}
	t.Cleanup(func() {
		if out, err := exec.Command("docker", "rm", "-f", name).CombinedOutput(); err != nil {
			t.Logf("could not remove %s: %v — %s", name, err, out)
		}
	})

	inventory := runDockerInventory(t, true, 62)

	if !inventory.StatsSampled {
		t.Fatalf("stats were requested and the inventory says none were sampled: %+v", inventory.Containers)
	}
	var found *DockerContainer
	for index := range inventory.Containers {
		if strings.Contains(inventory.Containers[index].Name, name) {
			found = &inventory.Containers[index]
		}
	}
	if found == nil {
		t.Fatalf("the sampled container is missing from the inventory: %+v", inventory.Containers)
	}
	// A RUNNING container has a memory limit, so this is the figure that proves the pair actually
	// parsed rather than that the fields exist. CPU may legitimately read 0.00% for a paused process —
	// which is exactly why it is a pointer and why zero is not treated as absence.
	if found.MemoryBytes == nil {
		t.Errorf("memory was not parsed for a running container: %+v", found)
	}
	if found.MemoryLimit == nil || (found.MemoryLimit != nil && *found.MemoryLimit <= 0) {
		t.Errorf("the memory limit parsed to %v, which no running container has", found.MemoryLimit)
	}
	if found.CPUPercent == nil {
		t.Errorf("cpu was not parsed for a running container: %+v", found)
	}
}

func TestRealDocker_ContainerActionChangesStateAndReportsBothSides(t *testing.T) {
	requireRealDocker(t)

	name := fmt.Sprintf("forgeops-act-%d", time.Now().UnixNano()%1_000_000)
	if out, err := exec.Command("docker", "run", "-d", "--name", name,
		"registry.k8s.io/pause:3.9").CombinedOutput(); err != nil {
		t.Fatalf("starting a container to act on: %v — %s", err, out)
	}
	t.Cleanup(func() {
		if out, err := exec.Command("docker", "rm", "-f", name).CombinedOutput(); err != nil {
			t.Logf("could not remove %s: %v — %s", name, err, out)
		}
	})

	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	act := func(action string, seq int64) DockerActionReport {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		result, err := d.Execute(ctx, verified(t, OpDockerContainerAction, "approval-real",
			map[string]any{"action": action, "container": name}, seq),
			SinkFunc(func(int, string, string) {}))
		if err != nil {
			t.Fatalf("docker %s against the real daemon: %v", action, err)
		}
		var report DockerActionReport
		if err := json.Unmarshal([]byte(result.Output), &report); err != nil {
			t.Fatalf("decoding the action report from %q: %v", result.Output, err)
		}
		return report
	}

	stopped := act("stop", 63)
	// THE POINT OF READING BOTH SIDES: the audit row records what changed, so a stop of an already
	// stopped container is distinguishable from a stop of a running one.
	if stopped.StateBefore != "running" {
		t.Errorf("state_before is %q, want running", stopped.StateBefore)
	}
	if stopped.StateAfter != "exited" {
		t.Errorf("state_after is %q, want exited", stopped.StateAfter)
	}

	// Read it back from the DAEMON, not from the report.
	out, err := exec.Command("docker", "inspect", "--format", "{{.State.Status}}", name).CombinedOutput()
	if err != nil {
		t.Fatalf("inspecting the container: %v — %s", err, out)
	}
	if got := strings.TrimSpace(string(out)); got != "exited" {
		t.Errorf("the daemon reports %q after a stop, want exited", got)
	}

	started := act("start", 64)
	if started.StateBefore != "exited" || started.StateAfter != "running" {
		t.Errorf("a start reported %q -> %q, want exited -> running",
			started.StateBefore, started.StateAfter)
	}
}

func TestRealDocker_AnAbsentTargetIsRefusedBeforeActing(t *testing.T) {
	requireRealDocker(t)

	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	_, err = d.Execute(ctx, verified(t, OpDockerContainerAction, "approval-real",
		map[string]any{"action": "stop", "container": "forgeops-no-such-container-9f2a"}, 65),
		SinkFunc(func(int, string, string) {}))
	if err == nil {
		t.Fatal("an action on a container that does not exist reported success")
	}
	// The refusal must NAME the container. "docker failed" for a typo sends an operator to the daemon's
	// logs instead of to their own spelling.
	if !strings.Contains(err.Error(), "forgeops-no-such-container-9f2a") {
		t.Errorf("the refusal does not name the missing container: %v", err)
	}
}

func TestRealDocker_TheOptInIsNamedInCI(t *testing.T) {
	body, err := os.ReadFile("../../../.github/workflows/k8s-ci.yml")
	if err != nil {
		t.Fatalf("reading the workflow: %v", err)
	}
	workflow := string(body)
	if !strings.Contains(workflow, realDockerEnv+": \"1\"") {
		t.Errorf("the workflow does not set %s=\"1\", so the real daemon is never exercised", realDockerEnv)
	}
	if !strings.Contains(workflow, "TestRealDocker") {
		t.Errorf("the workflow does not run TestRealDocker, so the opt-in is set and unused")
	}
}
