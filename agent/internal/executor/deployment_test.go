// SPDX-License-Identifier: Apache-2.0

// `deployment.apply_manifests`, and specifically the distinctions it exists to preserve.
//
// WHAT IS WORTH TESTING HERE WITHOUT A CLUSTER, and what honestly is not.
//
// A real `kubectl apply` against a real cluster belongs in the e2e stack; a fake `kubectl` on PATH would
// establish that this code can call a script, which is not a property anybody needs. What CAN be
// established here — and what every surface above this depends on — is the decision logic:
//
//   - a manifest set is confined to the workspace BEFORE any tool runs, so a signed envelope naming
//     `../../etc/passwd` is refused rather than applied;
//   - an empty set is refused instead of reported as a successful deployment of nothing;
//   - `kubectl`'s output is parsed into the workloads worth waiting for, and a Service is NOT one:
//     `rollout status` on a Service exits non-zero, so treating it as waitable would fail every manifest
//     set that contains one;
//   - the health-timeout clamp holds in both directions, and stays below the operation's own budget, or
//     "the workload did not become ready" surfaces as "the operation timed out" — a different fact with a
//     different remedy;
//   - `firstLine` returns the LAST non-empty line, because `kubectl rollout status` streams progress and
//     puts its verdict at the end. The literal first line would report "Waiting for deployment rollout to
//     finish" as the outcome of a rollout that then succeeded.
//
// The absence of a cluster is recorded in PROGRESS.md rather than hidden: this file does not claim the
// operation has been run against Kubernetes.

package executor

import (
	"testing"
	"time"
)

func TestParseAppliedResource(t *testing.T) {
	cases := []struct {
		name     string
		line     string
		wantKind string
		wantName string
	}{
		{"kubectl's usual shape", "deployment.apps/checkout-api created", "deployment", "checkout-api"},
		{"already unqualified", "deployment/checkout-api configured", "deployment", "checkout-api"},
		{"a service", "service/checkout-api unchanged", "service", "checkout-api"},
		{"a statefulset", "statefulset.apps/db created", "statefulset", "db"},
		{"leading and trailing space", "  daemonset.apps/node-agent created  ", "daemonset", "node-agent"},
		// An unparsable line yields an empty kind and is therefore never waited on. The safe direction:
		// the alternative is inventing a name and asking the cluster about something that is not there.
		{"a warning line", "Warning: resource is missing an annotation", "", ""},
		{"empty", "", "", ""},
		{"no slash", "deployment.apps checkout-api created", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			kind, name := parseAppliedResource(tc.line)
			if kind != tc.wantKind || name != tc.wantName {
				t.Fatalf("parseAppliedResource(%q) = (%q, %q), want (%q, %q)",
					tc.line, kind, name, tc.wantKind, tc.wantName)
			}
		})
	}
}

func TestOnlyPodBearingKindsAreWaitedOn(t *testing.T) {
	// Asserted in BOTH directions. A missing kind means a deployment reports healthy without having
	// waited for anything; an extra one means every manifest set containing a Service fails.
	for _, kind := range []string{"deployment", "statefulset", "daemonset"} {
		if _, ok := workloadKinds[kind]; !ok {
			t.Errorf("%q is not waited on, so deploying one would report healthy unverified", kind)
		}
	}
	for _, kind := range []string{"service", "configmap", "secret", "ingress", "namespace", "job"} {
		if _, ok := workloadKinds[kind]; ok {
			t.Errorf("%q is waited on, but `kubectl rollout status` has no rollout for it", kind)
		}
	}
	if len(workloadKinds) != 3 {
		t.Errorf("workloadKinds has %d entries, expected 3; update this count with the set", len(workloadKinds))
	}
}

func TestHealthTimeoutIsClampedInBothDirections(t *testing.T) {
	cases := []struct {
		name    string
		seconds int
		want    time.Duration
	}{
		{"absent falls back to the default", 0, DefaultHealthTimeout},
		{"negative falls back too", -30, DefaultHealthTimeout},
		{"an ordinary request is honoured", 90, 90 * time.Second},
		{"the ceiling is enforced", 3600, MaxHealthTimeout},
		{"exactly the ceiling", int(MaxHealthTimeout.Seconds()), MaxHealthTimeout},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := healthTimeout(tc.seconds); got != tc.want {
				t.Fatalf("healthTimeout(%d) = %s, want %s", tc.seconds, got, tc.want)
			}
		})
	}
	// The per-workload bound must stay below the operation's own budget, or a health timeout is reported
	// as an operation timeout and the reason is lost.
	if MaxHealthTimeout >= timeoutDeploy {
		t.Fatalf("MaxHealthTimeout (%s) must stay below the operation timeout (%s)",
			MaxHealthTimeout, timeoutDeploy)
	}
}

func TestFirstLineTakesTheVerdictAndNotTheProgress(t *testing.T) {
	streamed := "Waiting for deployment \"checkout-api\" rollout to finish: 0 of 3 updated replicas are available...\n" +
		"Waiting for deployment \"checkout-api\" rollout to finish: 2 of 3 updated replicas are available...\n" +
		"deployment \"checkout-api\" successfully rolled out\n"
	if got := firstLine(streamed); got != `deployment "checkout-api" successfully rolled out` {
		t.Fatalf("firstLine took %q, which is progress rather than the verdict", got)
	}
	if got := firstLine("   \n\n  \n"); got != "" {
		t.Fatalf("firstLine over whitespace = %q, want empty", got)
	}
	if got := firstLine("one line only"); got != "one line only" {
		t.Fatalf("firstLine single = %q", got)
	}
	// CRLF, because a Windows agent is a first-class host here.
	if got := firstLine("first\r\nlast\r\n"); got != "last" {
		t.Fatalf("firstLine CRLF = %q, want %q", got, "last")
	}
}
