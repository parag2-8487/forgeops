// SPDX-License-Identifier: Apache-2.0

// The Docker and Kubernetes operations, and specifically the distinctions they exist to preserve.
//
// WHAT IS WORTH TESTING WITHOUT A DAEMON OR A CLUSTER, and what is not. A fake `docker` on PATH would
// establish that this code can call a script. What matters, and what every panel above it depends on, is:
//
//   - the closed sets are closed, and an action outside them is refused BY NAME rather than passed to the
//     tool to reject;
//   - an empty target is refused rather than expanded. `docker rm` with no argument is an error, but a
//     wrapper that turned "" into "all" would be a host-wide delete from one malformed envelope;
//   - a workload action cannot name a kind the platform cannot VERIFY, because an unverifiable action
//     reports the API server's acceptance as success;
//   - a scale is bounded in both directions and the refusal names the bound;
//   - docker's `--format json` output is ONE OBJECT PER LINE and not an array, so a whole-buffer
//     unmarshal fails on the second container. A wrapper that read that failure as "no containers" would
//     report an empty host;
//   - every measured quantity distinguishes "not sampled" from zero. This is the defect class this phase
//     was warned about, in the one place a human reads a number and acts on it.
//
// The real daemon and the real cluster are exercised where they belong: `deployment_cluster_test.go`
// against kind in CI, and the e2e stack for the panels.

package executor

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

func TestContainerActionSetIsClosedAndComplete(t *testing.T) {
	// BOTH DIRECTIONS. A missing action means the dashboard offers a button that cannot work; an extra
	// one means authority nobody reviewed.
	for _, action := range []string{"start", "stop", "restart", "remove"} {
		if _, ok := containerActions[action]; !ok {
			t.Errorf("%q is not in the closed set, so the dashboard's button cannot work", action)
		}
	}
	if len(containerActions) != 4 {
		t.Errorf("the container action set has %d entries, expected 4: a new one is new authority and "+
			"belongs in design §7.7 in the same commit", len(containerActions))
	}
	// The specific absences that matter. Each of these would be a materially larger authority.
	for _, forbidden := range []string{"exec", "run", "create", "kill", "prune", "commit", "cp"} {
		if _, ok := containerActions[forbidden]; ok {
			t.Errorf("%q is reachable, which is a different authority from acting on one named container",
				forbidden)
		}
	}
}

func TestRemoveIsNotForced(t *testing.T) {
	// `docker rm` on a RUNNING container fails, and that refusal is the product: stopping something
	// still serving traffic is the operator's decision. A `-f` here would make it this code's.
	for action, vector := range containerActions {
		for _, argument := range vector {
			if argument == "-f" || argument == "--force" {
				t.Errorf("the %q action carries %q, which decides on the operator's behalf", action, argument)
			}
		}
	}
	for action, vector := range imageActions {
		for _, argument := range vector {
			if argument == "-f" || argument == "--force" {
				t.Errorf("the image %q action carries %q, which decides on the operator's behalf",
					action, argument)
			}
		}
	}
}

func TestImageActionSetIsClosed(t *testing.T) {
	for _, action := range []string{"pull", "remove"} {
		if _, ok := imageActions[action]; !ok {
			t.Errorf("%q is not in the closed image set", action)
		}
	}
	if len(imageActions) != 2 {
		t.Errorf("the image action set has %d entries, expected 2", len(imageActions))
	}
	// `prune` is the one worth naming: it deletes an unbounded set chosen by the daemon, which is
	// exactly the shape `deployment.apply_manifests` refuses `--prune` for.
	if _, ok := imageActions["prune"]; ok {
		t.Error("image prune is reachable: an unbounded delete nobody reviewed")
	}
}

func TestWorkloadActionSetIsClosedAndVerifiable(t *testing.T) {
	for _, action := range []string{"scale", "restart", "rollback"} {
		if _, ok := workloadActions[action]; !ok {
			t.Errorf("%q is not in the closed workload set", action)
		}
	}
	if len(workloadActions) != 3 {
		t.Errorf("the workload action set has %d entries, expected 3", len(workloadActions))
	}
	if _, ok := workloadActions["delete"]; ok {
		t.Error("delete is reachable from the dashboard's scale authority")
	}
	// THE COUPLING THAT MATTERS: the kinds this can act on are exactly the kinds the deployment
	// operation can wait for. If they ever diverge, an action becomes unverifiable and would report the
	// API server's acceptance as convergence.
	for kind := range workloadKinds {
		if kind != "deployment" && kind != "statefulset" && kind != "daemonset" {
			t.Errorf("%q is waitable but not one of the three pod-bearing kinds", kind)
		}
	}
	if len(workloadKinds) != 3 {
		t.Errorf("the waitable set has %d kinds, expected 3; the action operation shares this set, so "+
			"widening it widens what may be acted on", len(workloadKinds))
	}
}

func TestScaleBoundIsNamedAndFinite(t *testing.T) {
	if MaxScaleReplicas <= 0 {
		t.Fatal("the scale bound is not positive, so no scale could succeed")
	}
	if MaxScaleReplicas > 1000 {
		t.Errorf("the scale bound is %d, which is not a bound a cluster survives being asked for",
			MaxScaleReplicas)
	}
}

func TestDockerJSONLinesAreDecodedOnePerLine(t *testing.T) {
	// The real shape of `docker ps --format json`: one object per line, no array, no commas. A
	// whole-buffer unmarshal fails on the second line, and treating that as "no containers" would
	// report an empty host to somebody debugging a full one.
	output := `{"ID":"aaa","Names":"api","State":"running"}
{"ID":"bbb","Names":"db","State":"exited"}

{"ID":"ccc","Names":"cache","State":"running"}`

	var names []string
	err := decodeJSONLines(output, func(raw json.RawMessage) error {
		var row struct {
			Names string `json:"Names"`
		}
		if err := json.Unmarshal(raw, &row); err != nil {
			return err
		}
		names = append(names, row.Names)
		return nil
	})
	if err != nil {
		t.Fatalf("decoding docker's real output shape: %v", err)
	}
	if len(names) != 3 {
		t.Fatalf("decoded %d container(s) from three lines: %v", len(names), names)
	}
	// A blank line between objects is normal and must not end the read.
	if names[2] != "cache" {
		t.Errorf("a blank line truncated the list: %v", names)
	}
}

func TestDockerJSONLinesReportsAnUnreadableLine(t *testing.T) {
	// The failure must be an ERROR, not an empty list. Silence here is what turns a parsing bug into a
	// dashboard that says the host has nothing running.
	err := decodeJSONLines(`{"ID":"aaa"}`+"\n"+`this is not json`, func(raw json.RawMessage) error {
		var row struct {
			ID string `json:"ID"`
		}
		return json.Unmarshal(raw, &row)
	})
	if err == nil {
		t.Fatal("an unreadable line was accepted, so a parse failure would render as an empty host")
	}
	if !strings.Contains(err.Error(), "this is not json") {
		t.Errorf("the error does not quote what it could not read: %v", err)
	}
}

func TestStatsParsingDistinguishesUnmeasuredFromZero(t *testing.T) {
	// THE TRI-STATE, at the level of one number. A dashboard that rendered nil and 0 alike would show
	// an unmeasured container as idle, and somebody would act on that.
	if got := parseStatPercent("0.00%"); got == nil || *got != 0 {
		t.Errorf("a real zero reading parsed to %v, want a pointer to 0", got)
	}
	if got := parseStatPercent(""); got != nil {
		t.Errorf("an absent reading parsed to %v, want nil", got)
	}
	if got := parseStatPercent("--"); got != nil {
		t.Errorf("an unreadable reading parsed to %v, want nil", got)
	}
	if got := parseStatPercent("12.34%"); got == nil || *got < 12.3 || *got > 12.4 {
		t.Errorf("12.34%% parsed to %v", got)
	}
}

func TestByteSizeParsingCoversWhatDockerPrints(t *testing.T) {
	cases := map[string]float64{
		"1.5MiB": 1.5 * 1024 * 1024,
		"2GiB":   2 * 1024 * 1024 * 1024,
		"512B":   512,
		"3.2kB":  3200,
	}
	for input, want := range cases {
		got := parseByteSize(input)
		if got == nil {
			t.Errorf("%q parsed to nil", input)
			continue
		}
		if *got < want*0.999 || *got > want*1.001 {
			t.Errorf("%q parsed to %v, want about %v", input, *got, want)
		}
	}
	for _, input := range []string{"", "--", "1.5PiB", "not a size"} {
		if got := parseByteSize(input); got != nil {
			t.Errorf("%q parsed to %v, want nil so the panel reports it as unmeasured", input, got)
		}
	}
}

func TestMemoryPairSplitsUsedFromLimit(t *testing.T) {
	used, limit := parseStatBytes("1.5MiB / 2GiB")
	if used == nil || limit == nil {
		t.Fatalf("a real docker memory pair parsed to (%v, %v)", used, limit)
	}
	if *used >= *limit {
		t.Errorf("used %v is not below limit %v", *used, *limit)
	}
	// A pair this code cannot split leaves BOTH nil rather than guessing which half it read.
	if u, l := parseStatBytes("1.5MiB"); u != nil || l != nil {
		t.Errorf("an unsplittable pair parsed to (%v, %v), want both nil", u, l)
	}
}

func TestDockerContainerActionRefusesAnEmptyTarget(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// THE CATASTROPHIC DEFAULT this refuses: an action with no target must not become an action on
	// every target. Checked before Docker is even looked for, so the refusal does not depend on the
	// host having a daemon.
	_, err = d.Execute(context.Background(), verified(t, OpDockerContainerAction, "approval-1",
		map[string]any{"action": "remove", "container": ""}, 41), nil)
	if err == nil {
		t.Fatal("an action naming no container was accepted")
	}
	if Code(err) == "" {
		t.Errorf("the refusal carries no code: %v", err)
	}
}

func TestDockerContainerActionRefusesAnUnknownAction(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for _, action := range []string{"exec", "prune", "", "RM", "stop; rm -rf /"} {
		_, err := d.Execute(context.Background(), verified(t, OpDockerContainerAction, "approval-1",
			map[string]any{"action": action, "container": "api"}, 42), nil)
		if err == nil {
			t.Errorf("the action %q was accepted", action)
			continue
		}
		// It must be refused as an UNKNOWN ACTION, not reported as a docker failure: the two send an
		// operator to different places.
		if !strings.Contains(err.Error(), "closed set") {
			t.Errorf("%q was refused for the wrong reason: %v", action, err)
		}
	}
}

func TestWorkloadActionRefusesAnUnverifiableKind(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// A Job, a CronJob and a Pod are all real Kubernetes things and none of them is something
	// `rollout status` can verify. Accepting one would mean reporting acceptance as convergence.
	for _, kind := range []string{"job", "cronjob", "pod", "service", "namespace", "secret"} {
		_, err := d.Execute(context.Background(), verified(t, OpKubernetesWorkloadAction, "approval-1",
			map[string]any{"action": "restart", "kind": kind, "name": "x", "namespace": "default"}, 43), nil)
		if err == nil {
			t.Errorf("a %q was accepted as a workload action target", kind)
		}
	}
}

func TestWorkloadActionRefusesAScaleOutsideTheBound(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for _, replicas := range []int{-1, MaxScaleReplicas + 1, 10_000} {
		_, err := d.Execute(context.Background(), verified(t, OpKubernetesWorkloadAction, "approval-1",
			map[string]any{"action": "scale", "kind": "deployment", "name": "api",
				"namespace": "default", "replicas": replicas}, 44), nil)
		if err == nil {
			t.Errorf("a scale to %d was accepted", replicas)
			continue
		}
		// The bound must be NAMED. A refusal that does not say what the limit is leaves an operator
		// guessing, and guessing at a limit means trying again with another wrong number.
		if !strings.Contains(err.Error(), "allowed") {
			t.Errorf("the refusal of %d does not name the bound: %v", replicas, err)
		}
	}
}

func TestWorkloadActionNeedsBothNamespaceAndName(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for _, args := range []map[string]any{
		{"action": "restart", "kind": "deployment", "name": "", "namespace": "default"},
		{"action": "restart", "kind": "deployment", "name": "api", "namespace": ""},
	} {
		if _, err := d.Execute(context.Background(), verified(t, OpKubernetesWorkloadAction, "approval-1",
			args, 45), nil); err == nil {
			t.Errorf("%v was accepted; an unqualified workload name is not one workload", args)
		}
	}
}

func TestRestartIsARolloutAndNotAPodDelete(t *testing.T) {
	// Read from the source of the vector rather than by running it: the property is which subcommand is
	// built, and a pod delete bypasses the workload's surge and availability settings, which is how a
	// "restart" becomes an outage.
	//
	// Asserted through the action set's own shape: `delete` is not reachable at all, and the restart
	// path is `rollout restart`. The vector is built inside the handler, so the guard here is that
	// nothing in the closed set can express a delete.
	if _, ok := workloadActions["delete"]; ok {
		t.Fatal("delete is in the workload action set")
	}
	for action := range workloadActions {
		if strings.Contains(action, "delete") || strings.Contains(action, "kill") {
			t.Errorf("%q can remove a running workload from a dashboard's restart button", action)
		}
	}
}

func TestInventoriesAreNeitherMutatingNorApprovalRequiring(t *testing.T) {
	// The pairing that makes a refreshing dashboard possible, asserted where it can regress. If either
	// of these became mutating, every panel refresh would need an approval; if either of the ACTION
	// operations became non-mutating, a dashboard could change a host without one.
	for _, op := range []Operation{OpDockerInventory, OpKubernetesInventory} {
		row := handlerTable[op]
		if row.mutating {
			t.Errorf("%q is mutating, so a dashboard refresh would mint approvals", op)
		}
		if row.requiresApproval {
			t.Errorf("%q requires an approval, so a dashboard could not refresh", op)
		}
		if !row.implemented {
			t.Errorf("%q is catalogued but has no body", op)
		}
	}
	for _, op := range []Operation{OpDockerContainerAction, OpDockerImageAction, OpKubernetesWorkloadAction} {
		row := handlerTable[op]
		if !row.mutating || !row.requiresApproval {
			t.Errorf("%q changes what is running and must be mutating and approval-requiring", op)
		}
	}
}

func TestDecodeFirstJSONSurvivesAWarningOnEitherSide(t *testing.T) {
	// THE REAL OUTPUTS that broke two successive versions of this code, and neither was reachable without
	// a real cluster. kubectl writes its skew warning to stderr and the runner merges the streams, so the
	// warning lands BEFORE the document or AFTER it depending on which stream flushes first. Trimming to
	// the first brace fixes only the first case; the second fails with "invalid character 'W' after
	// top-level value", which is what actually happened.
	const warning = "Warning: version difference between client (1.36) and server (1.32) exceeds the " +
		"supported minor version skew of +/-1"
	const document = `{"serverVersion":{"gitVersion":"v1.32.2"}}`

	for name, buffer := range map[string]string{
		"before": warning + "\n" + document,
		"after":  document + "\n" + warning,
		"both":   warning + "\n" + document + "\n" + warning,
		"clean":  document,
	} {
		var payload struct {
			ServerVersion struct {
				GitVersion string `json:"gitVersion"`
			} `json:"serverVersion"`
		}
		if err := decodeFirstJSON(buffer, &payload); err != nil {
			t.Errorf("%s: a warning made the document unreadable: %v", name, err)
			continue
		}
		if payload.ServerVersion.GitVersion != "v1.32.2" {
			t.Errorf("%s: parsed the version as %q", name, payload.ServerVersion.GitVersion)
		}
	}

	// A buffer with no document at all must REPORT that, with what the tool said, rather than yield an
	// empty result that a panel would render as "this cluster holds nothing".
	var ignored struct{}
	err := decodeFirstJSON("error: the server could not find the requested resource", &ignored)
	if err == nil {
		t.Fatal("a buffer with no JSON decoded successfully")
	}
	if !strings.Contains(err.Error(), "error: the server") {
		t.Errorf("the failure does not quote what the tool said: %v", err)
	}
}

func TestBothInventoriesRefuseMalformedArgumentsBeforeTouchingATool(t *testing.T) {
	// The refusal happens before Docker or kubectl is looked for, so it holds on a machine with neither --
	// and it must report `envelope-malformed` rather than "the daemon did not answer", because a
	// mis-assembled envelope and an absent daemon send an operator to opposite places.
	//
	// THE PAYLOAD IS PER-OPERATION, and the first version of this test got that wrong in an instructive
	// way: it sent {"stats": "yes please"} to both. Docker refused it, because `stats` is a bool there --
	// and Kubernetes ACCEPTED it, correctly, because `stats` is not a field it has and Go ignores unknown
	// keys. A malformed object has to be malformed for the struct being decoded, so each one below carries
	// a wrong TYPE on a field that operation really declares.
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	cases := map[Operation]json.RawMessage{
		OpDockerInventory:     json.RawMessage(`{"stats": "yes please"}`),
		OpKubernetesInventory: json.RawMessage(`{"namespace": 12}`),
	}
	seq := int64(91)
	for operation, args := range cases {
		seq++
		_, err := d.Execute(context.Background(), verifiedRaw(t, operation, "", args, seq), nil)
		if err == nil {
			t.Errorf("%s accepted a malformed argument object", operation)
			continue
		}
		if Code(err) != "envelope-malformed" {
			t.Errorf("%s refused with code %q, want envelope-malformed: %v", operation, Code(err), err)
		}
	}
}

func TestImageActionRefusesAnEmptyOrUnknownTargetBeforeTouchingADaemon(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	cases := []struct {
		name string
		args map[string]any
		want string
	}{
		{"no image named", map[string]any{"action": "pull", "image": ""}, "no target"},
		{"only whitespace", map[string]any{"action": "remove", "image": "   "}, "no target"},
		{"an action outside the set", map[string]any{"action": "build", "image": "nginx:1.27"}, "closed set"},
		{"push is not reachable", map[string]any{"action": "push", "image": "nginx:1.27"}, "closed set"},
		{"prune is not reachable", map[string]any{"action": "prune", "image": "nginx:1.27"}, "closed set"},
	}
	for index, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := d.Execute(context.Background(),
				verified(t, OpDockerImageAction, "approval-1", tc.args, int64(92+index)), nil)
			if err == nil {
				t.Fatalf("%v was accepted", tc.args)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("refused for the wrong reason: %v", err)
			}
		})
	}
}

func TestWorkloadActionRefusesMalformedArgumentsBeforeTouchingACluster(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	_, err = d.Execute(context.Background(),
		verifiedRaw(t, OpKubernetesWorkloadAction, "approval-1", []byte(`{"replicas": "three"}`), 97), nil)
	if err == nil {
		t.Fatal("a malformed workload action was accepted")
	}
	if Code(err) != "envelope-malformed" {
		t.Errorf("refused with code %q, want envelope-malformed: %v", Code(err), err)
	}
}

// verifiedRaw is `verified` for an argument object that cannot be produced by marshalling a Go value ?
// specifically, one whose FIELD TYPES are wrong. Testing the decode failure requires sending bytes the
// struct cannot accept, and `json.Marshal` of a valid map can never produce them.
func verifiedRaw(t *testing.T, op Operation, approvalID string, args json.RawMessage, seq int64) *envelope.Verified {
	t.Helper()
	return verified(t, op, approvalID, args, seq)
}
