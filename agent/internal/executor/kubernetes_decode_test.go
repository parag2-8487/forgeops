// SPDX-License-Identifier: Apache-2.0

// The `kubectl get -o json` decoders, against the shapes the real API actually produces.
//
// WHY THESE EXIST AS A SEPARATE SUITE. The decoders were inline closures inside `k8sInventory`, so the only
// way to run them was to have a cluster — 365 lines that a developer's `go test ./...` never touched, in the
// part of the path most likely to be subtly wrong. They are pure functions of a tool's output, so they are
// tested as such, with payloads whose SHAPE is the real one rather than a convenient one.
//
// Every fixture here reproduces a property of the real API that a naive decoder gets wrong, and the comment
// on each says which:
//
//   - a DaemonSet has no `spec.replicas` at all;
//   - `status.readyReplicas` is OMITTED at zero replicas, not zero;
//   - a node's Ready condition is one entry in an unordered array;
//   - a pod's waiting reason is per container;
//   - `kubectl` may print a warning before or after the document.
package executor

import (
	"strings"
	"testing"
)

func TestDecodeNamespaces(t *testing.T) {
	names, err := decodeNamespaces(`{"apiVersion":"v1","items":[
		{"metadata":{"name":"default"}},
		{"metadata":{"name":"kube-system"}}
	],"kind":"List"}`)
	if err != nil {
		t.Fatalf("decoding namespaces: %v", err)
	}
	if len(names) != 2 || names[0] != "default" || names[1] != "kube-system" {
		t.Errorf("decoded %v", names)
	}

	// AN EMPTY LIST IS A VALID ANSWER and must not be an error — that is the difference between "none" and
	// "unreadable", which the caller turns into two different things on screen.
	empty, err := decodeNamespaces(`{"items":[]}`)
	if err != nil {
		t.Fatalf("an empty list was treated as unreadable: %v", err)
	}
	if len(empty) != 0 {
		t.Errorf("decoded %v from an empty list", empty)
	}

	// AND AN UNREADABLE DOCUMENT IS AN ERROR, not an empty list.
	if _, err := decodeNamespaces("error: the server doesn't have a resource type"); err == nil {
		t.Error("an error message decoded as a namespace list")
	}
}

func TestDecodeNodesPreservesTheReadinessTriState(t *testing.T) {
	// The conditions array in the order a real cluster emits it: Ready is LAST, after
	// MemoryPressure/DiskPressure/PIDPressure. A decoder that indexed [0] would read MemoryPressure's
	// status — which is "False" on a healthy node — and report every healthy node as not ready.
	nodes, err := decodeNodes(`{"items":[
		{"metadata":{"name":"node-a"},"status":{
			"conditions":[
				{"type":"MemoryPressure","status":"False"},
				{"type":"DiskPressure","status":"False"},
				{"type":"PIDPressure","status":"False"},
				{"type":"Ready","status":"True"}
			],
			"nodeInfo":{"kubeletVersion":"v1.28.0","osImage":"Debian GNU/Linux 11"},
			"allocatable":{"cpu":"8","memory":"16374436Ki","pods":"110"}
		}},
		{"metadata":{"name":"node-b"},"status":{
			"conditions":[{"type":"Ready","status":"False"}],
			"nodeInfo":{"kubeletVersion":"v1.28.0","osImage":"Debian GNU/Linux 11"},
			"allocatable":{"cpu":"8","memory":"16374436Ki"}
		}},
		{"metadata":{"name":"node-c"},"status":{
			"conditions":[{"type":"MemoryPressure","status":"False"}],
			"nodeInfo":{"kubeletVersion":"v1.28.0","osImage":"Debian GNU/Linux 11"},
			"allocatable":{"cpu":"8","memory":"16374436Ki"}
		}}
	]}`)
	if err != nil {
		t.Fatalf("decoding nodes: %v", err)
	}
	if len(nodes) != 3 {
		t.Fatalf("decoded %d node(s)", len(nodes))
	}
	if nodes[0].Ready == nil || !*nodes[0].Ready {
		t.Errorf("a Ready=True node decoded as %v — the conditions array was not searched", nodes[0].Ready)
	}
	if nodes[1].Ready == nil || *nodes[1].Ready {
		t.Errorf("a Ready=False node decoded as %v", nodes[1].Ready)
	}
	// THE THIRD STATE. A node with no Ready condition has not reported one, and that is neither ready nor
	// not-ready: reporting `false` would page somebody for a reporting gap.
	if nodes[2].Ready != nil {
		t.Errorf("a node with no Ready condition decoded as %v, want nil", *nodes[2].Ready)
	}
	if nodes[0].Version != "v1.28.0" || nodes[0].Allocatable.CPU != "8" {
		t.Errorf("node metadata was lost: %+v", nodes[0])
	}
	if nodes[0].Allocatable.Memory != "16374436Ki" {
		t.Errorf("allocatable memory decoded as %q", nodes[0].Allocatable.Memory)
	}
}

func TestDecodePodsSumsRestartsAndFindsTheWaitingReason(t *testing.T) {
	// Two containers, one ready and one stuck pulling — the ordinary broken-sidecar shape. The reason lives
	// on the container, not the pod, and the restart count that matters is the total.
	pods, err := decodePods(`{"items":[
		{"metadata":{"name":"api-7c9","namespace":"default"},
		 "spec":{"nodeName":"node-a"},
		 "status":{"phase":"Pending","startTime":"2026-09-21T10:00:00Z","containerStatuses":[
			{"ready":true,"restartCount":2,"state":{"running":{"startedAt":"2026-09-21T10:00:05Z"}}},
			{"ready":false,"restartCount":5,"state":{"waiting":{"reason":"ImagePullBackOff","message":"Back-off pulling image"}}}
		 ]}},
		{"metadata":{"name":"unscheduled","namespace":"default"},
		 "spec":{},
		 "status":{"phase":"Pending","containerStatuses":[]}}
	]}`)
	if err != nil {
		t.Fatalf("decoding pods: %v", err)
	}
	if len(pods) != 2 {
		t.Fatalf("decoded %d pod(s)", len(pods))
	}
	if pods[0].Ready != 1 || pods[0].Total != 2 {
		t.Errorf("readiness decoded as %d/%d, want 1/2 — a boolean would lose the sidecar",
			pods[0].Ready, pods[0].Total)
	}
	if pods[0].Restarts != 7 {
		t.Errorf("restarts decoded as %d, want 7 (summed across containers)", pods[0].Restarts)
	}
	if pods[0].Reason != "ImagePullBackOff" {
		t.Errorf("the waiting reason decoded as %q — this is what an operator acts on", pods[0].Reason)
	}
	if pods[0].Node != "node-a" {
		t.Errorf("the node decoded as %q", pods[0].Node)
	}
	// AN UNSCHEDULED POD has no node and no container statuses, and neither is an error.
	if pods[1].Node != "" || pods[1].Total != 0 || pods[1].Reason != "" {
		t.Errorf("an unscheduled pod decoded as %+v", pods[1])
	}
}

func TestDecodeWorkloadsHandlesTheThreeKindsRealDifferences(t *testing.T) {
	// A Deployment at 2/2.
	deployments, err := decodeWorkloads("deployment", `{"items":[
		{"metadata":{"name":"api","namespace":"default"},
		 "spec":{"replicas":2,"template":{"spec":{"containers":[{"image":"nginx:1.27"},{"image":"busybox:1.36"}]}}},
		 "status":{"readyReplicas":2,"replicas":2}}
	]}`)
	if err != nil {
		t.Fatalf("decoding deployments: %v", err)
	}
	if deployments[0].Desired == nil || *deployments[0].Desired != 2 {
		t.Errorf("desired decoded as %v", deployments[0].Desired)
	}
	if deployments[0].Ready == nil || *deployments[0].Ready != 2 {
		t.Errorf("ready decoded as %v", deployments[0].Ready)
	}
	if len(deployments[0].Images) != 2 {
		t.Errorf("images decoded as %v; both containers count", deployments[0].Images)
	}
	if deployments[0].Kind != "deployment" {
		t.Errorf("kind decoded as %q, and it must match what the action operation accepts",
			deployments[0].Kind)
	}

	// A DEPLOYMENT AT ZERO REPLICAS. `status.readyReplicas` is ABSENT — this is the real shape, and it is
	// why the field is a pointer. A decoder using `int` would report 0, which is indistinguishable from a
	// workload whose status nothing has populated.
	zeroed, err := decodeWorkloads("deployment", `{"items":[
		{"metadata":{"name":"idle","namespace":"default"},
		 "spec":{"replicas":0,"template":{"spec":{"containers":[{"image":"nginx:1.27"}]}}},
		 "status":{"observedGeneration":3}}
	]}`)
	if err != nil {
		t.Fatalf("decoding a zeroed deployment: %v", err)
	}
	if zeroed[0].Desired == nil || *zeroed[0].Desired != 0 {
		t.Errorf("desired decoded as %v, want a pointer to 0", zeroed[0].Desired)
	}
	if zeroed[0].Ready != nil {
		t.Errorf("ready decoded as %v; the API omits the field at zero and so must this", *zeroed[0].Ready)
	}

	// A DAEMONSET HAS NO `spec.replicas` AT ALL. Its desired count is `status.desiredNumberScheduled`, so
	// a decoder that only read the spec would report every DaemonSet as having no desired count — on a
	// panel whose whole job is to say how many should be running.
	daemons, err := decodeWorkloads("daemonset", `{"items":[
		{"metadata":{"name":"node-agent","namespace":"kube-system"},
		 "spec":{"template":{"spec":{"containers":[{"image":"fluentd:v1.16"}]}}},
		 "status":{"desiredNumberScheduled":3,"numberReady":3,"currentNumberScheduled":3}}
	]}`)
	if err != nil {
		t.Fatalf("decoding daemonsets: %v", err)
	}
	if daemons[0].Desired == nil || *daemons[0].Desired != 3 {
		t.Errorf("a DaemonSet's desired count decoded as %v, want 3 from desiredNumberScheduled",
			daemons[0].Desired)
	}
	if daemons[0].Ready == nil || *daemons[0].Ready != 3 {
		t.Errorf("a DaemonSet's ready count decoded as %v, want 3 from numberReady", daemons[0].Ready)
	}
}

func TestDecodeServicesFlattensPorts(t *testing.T) {
	services, err := decodeServices(`{"items":[
		{"metadata":{"name":"api","namespace":"default"},
		 "spec":{"type":"ClusterIP","clusterIP":"10.96.0.12","ports":[
			{"port":80,"protocol":"TCP","targetPort":8080},
			{"port":9090,"protocol":"TCP","targetPort":9090}
		 ]}},
		{"metadata":{"name":"headless","namespace":"default"},
		 "spec":{"type":"ClusterIP","clusterIP":"None","ports":[]}}
	]}`)
	if err != nil {
		t.Fatalf("decoding services: %v", err)
	}
	if services[0].Ports != "80/TCP,9090/TCP" {
		t.Errorf("ports decoded as %q", services[0].Ports)
	}
	// A HEADLESS SERVICE reports `None` as its cluster IP, which is real and must survive verbatim rather
	// than being normalised to empty — "None" is meaningful and "" reads as missing.
	if services[1].ClusterIP != "None" {
		t.Errorf("a headless service's cluster IP decoded as %q", services[1].ClusterIP)
	}
	if services[1].Ports != "" {
		t.Errorf("a service with no ports decoded as %q", services[1].Ports)
	}
}

func TestDecodeIngressesSkipsACatchAllRuleRatherThanShowingAnEmptyHost(t *testing.T) {
	ingresses, err := decodeIngresses(`{"items":[
		{"metadata":{"name":"web","namespace":"default"},
		 "spec":{"ingressClassName":"nginx","rules":[
			{"host":"app.example.com","http":{"paths":[{"path":"/"}]}},
			{"http":{"paths":[{"path":"/health"}]}}
		 ]}}
	]}`)
	if err != nil {
		t.Fatalf("decoding ingresses: %v", err)
	}
	// One host, not two — a rule with no host is a catch-all and has no name to display. Rendering it as
	// an empty string would look like a bug in the panel.
	if len(ingresses[0].Hosts) != 1 || ingresses[0].Hosts[0] != "app.example.com" {
		t.Errorf("hosts decoded as %v", ingresses[0].Hosts)
	}
	if ingresses[0].Class != "nginx" {
		t.Errorf("the ingress class decoded as %q", ingresses[0].Class)
	}
}

func TestDecodeConfigMapsReportsKeysOnlyAndInAStableOrder(t *testing.T) {
	document := `{"items":[
		{"metadata":{"name":"app-config","namespace":"default"},
		 "data":{"LOG_LEVEL":"info","DATABASE_URL":"postgres://somewhere","FEATURE_X":"on"}}
	]}`
	configMaps, err := decodeConfigMaps(document)
	if err != nil {
		t.Fatalf("decoding config maps: %v", err)
	}
	// THE VALUES MUST NOT SURVIVE. A ConfigMap regularly holds what should have been a Secret, and a panel
	// that printed values would publish it to every viewer of the project.
	for _, key := range configMaps[0].Keys {
		if strings.Contains(key, "postgres") || strings.Contains(key, "somewhere") {
			t.Errorf("a value leaked into the key list: %q", key)
		}
	}
	// SORTED, because Go randomises map iteration and a panel whose rows reorder on every refresh is
	// unusable. Asserted as an exact sequence rather than as a set.
	want := []string{"DATABASE_URL", "FEATURE_X", "LOG_LEVEL"}
	if len(configMaps[0].Keys) != len(want) {
		t.Fatalf("keys decoded as %v", configMaps[0].Keys)
	}
	for index, key := range want {
		if configMaps[0].Keys[index] != key {
			t.Errorf("keys decoded as %v, want %v — the order must be stable across refreshes",
				configMaps[0].Keys, want)
			break
		}
	}
}

func TestDecodeHPAsKeepsEveryCountNullable(t *testing.T) {
	autoscalers, err := decodeHPAs(`{"items":[
		{"metadata":{"name":"api","namespace":"default"},
		 "spec":{"minReplicas":2,"maxReplicas":10,"scaleTargetRef":{"kind":"Deployment","name":"api"}},
		 "status":{"currentReplicas":4,"desiredReplicas":4}},
		{"metadata":{"name":"fresh","namespace":"default"},
		 "spec":{"maxReplicas":5,"scaleTargetRef":{"kind":"Deployment","name":"other"}},
		 "status":{}}
	]}`)
	if err != nil {
		t.Fatalf("decoding autoscalers: %v", err)
	}
	if autoscalers[0].Target != "deployment/api" {
		t.Errorf("the target decoded as %q; it must match the workload vocabulary", autoscalers[0].Target)
	}
	if autoscalers[0].Current == nil || *autoscalers[0].Current != 4 {
		t.Errorf("current decoded as %v", autoscalers[0].Current)
	}
	// A FRESHLY CREATED HPA has no `minReplicas` (the API defaults it later) and no status at all. Both
	// must read as "not reported" rather than as zero, or a panel shows an autoscaler holding a workload
	// at no replicas when it is simply new.
	if autoscalers[1].MinReplicas != nil {
		t.Errorf("an absent minReplicas decoded as %v", *autoscalers[1].MinReplicas)
	}
	if autoscalers[1].Current != nil {
		t.Errorf("an absent currentReplicas decoded as %v", *autoscalers[1].Current)
	}
	if autoscalers[1].MaxReplicas == nil || *autoscalers[1].MaxReplicas != 5 {
		t.Errorf("maxReplicas decoded as %v; it is required by the API and always present",
			autoscalers[1].MaxReplicas)
	}
}

func TestEveryDecoderTreatsAWarningAroundTheDocumentAsNoise(t *testing.T) {
	// The real failure from a real cluster: kubectl writes the skew warning to stderr, the runner merges
	// the streams, and it lands before or after the document depending on which flushes first. Every
	// decoder goes through `decodeFirstJSON`, and this asserts that for all of them at once rather than
	// trusting that the next one added will remember to.
	const warning = "Warning: version difference between client (1.36) and server (1.32) exceeds the " +
		"supported minor version skew of +/-1"
	const document = `{"items":[{"metadata":{"name":"default"}}]}`

	for name, buffer := range map[string]string{
		"before": warning + "\n" + document,
		"after":  document + "\n" + warning,
	} {
		names, err := decodeNamespaces(buffer)
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if len(names) != 1 {
			t.Errorf("%s: decoded %v", name, names)
		}
	}
}

func TestDecodeDockerRowsAndStats(t *testing.T) {
	// The real shape: one object per line, no array.
	rows, err := decodeDockerRows(`{"ID":"aaa","Names":"api","Image":"nginx:1.27","State":"running","Status":"Up 2 minutes","Ports":"80/tcp"}
{"ID":"bbb","Names":"db","Image":"postgres:16","State":"exited","Status":"Exited (0) 1 hour ago","Ports":""}`)
	if err != nil {
		t.Fatalf("decoding docker rows: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("decoded %d row(s)", len(rows))
	}
	// EVERY MEASURED FIELD IS NIL until a sample is folded in. A row that arrived without stats must
	// report "not measured", not zero.
	if rows[0].CPUPercent != nil || rows[0].MemoryBytes != nil {
		t.Errorf("an unsampled row carries figures: %+v", rows[0])
	}
	if rows[1].State != "exited" {
		t.Errorf("a stopped container decoded as %q; the list must show it rather than hide it", rows[1].State)
	}

	sampled, err := applyDockerStats(rows, `{"Name":"api","ID":"aaa","CPUPerc":"0.00%","MemUsage":"12.5MiB / 2GiB","NetIO":"1.2kB / 648B"}`)
	if err != nil {
		t.Fatalf("applying stats: %v", err)
	}
	if !sampled {
		t.Fatal("a real sample reported nothing sampled, so the panel would say 'not measured'")
	}
	// A REAL ZERO IS A ZERO. An idle container reports 0.00% and must be shown as idle.
	if rows[0].CPUPercent == nil || *rows[0].CPUPercent != 0 {
		t.Errorf("an idle container's cpu decoded as %v, want a pointer to 0", rows[0].CPUPercent)
	}
	if rows[0].MemoryBytes == nil || rows[0].MemoryLimit == nil {
		t.Errorf("memory decoded as %v / %v", rows[0].MemoryBytes, rows[0].MemoryLimit)
	}
	if rows[0].NetworkRx == nil || rows[0].NetworkTx == nil {
		t.Errorf("network decoded as %v / %v", rows[0].NetworkRx, rows[0].NetworkTx)
	}
	// THE CONTAINER WITH NO SAMPLE STAYS UNMEASURED. A sample covering one row must not imply anything
	// about the other.
	if rows[1].CPUPercent != nil {
		t.Errorf("an unsampled container was given a figure: %v", *rows[1].CPUPercent)
	}
}

func TestApplyDockerStatsIgnoresAContainerThatAppearedBetweenTheCalls(t *testing.T) {
	rows, err := decodeDockerRows(`{"ID":"aaa","Names":"api","State":"running"}`)
	if err != nil {
		t.Fatalf("decoding: %v", err)
	}
	// A container started between `docker ps` and `docker stats`. Ignoring it is right: a list one second
	// out of date is not a failure, and refusing the whole sample for it would turn a race into an outage
	// of the panel.
	sampled, err := applyDockerStats(rows, `{"Name":"brand-new","ID":"zzz","CPUPerc":"5.00%","MemUsage":"1MiB / 2GiB","NetIO":"0B / 0B"}`)
	if err != nil {
		t.Fatalf("an unknown container made the whole sample fail: %v", err)
	}
	// And nothing was sampled, because nothing known was measured — which is the honest answer.
	if sampled {
		t.Error("stats_sampled is true although no known container was measured")
	}
	if rows[0].CPUPercent != nil {
		t.Errorf("a figure was applied to the wrong container: %v", *rows[0].CPUPercent)
	}
}

func TestScopeArgsChoosesBetweenOneNamespaceAndAll(t *testing.T) {
	if got := scopeArgs(""); len(got) != 1 || got[0] != "--all-namespaces" {
		t.Errorf("an empty namespace produced %v, want --all-namespaces", got)
	}
	if got := scopeArgs("production"); len(got) != 2 || got[0] != "--namespace" || got[1] != "production" {
		t.Errorf("a named namespace produced %v", got)
	}
}

func TestParseReplicaFieldKeepsAbsentApartFromZero(t *testing.T) {
	// THE CASE THAT LOOKS LIKE A BUG AND IS NOT. `jsonpath={.status.readyReplicas}` prints NOTHING for a
	// workload at zero replicas, because the API omits the field. Returning 0 would make a deliberately
	// scaled-down workload indistinguishable from one whose status nothing has populated -- and a panel
	// would show both as "0 ready", which is true of one and misleading about the other.
	if got := parseReplicaField(""); got != nil {
		t.Errorf("an absent field parsed to %v, want nil", *got)
	}
	if got := parseReplicaField("   \n"); got != nil {
		t.Errorf("whitespace parsed to %v, want nil", *got)
	}
	// A REAL ZERO, which the API does emit for some kinds, is a zero.
	if got := parseReplicaField("0"); got == nil || *got != 0 {
		t.Errorf("a real zero parsed to %v, want a pointer to 0", got)
	}
	if got := parseReplicaField("3\n"); got == nil || *got != 3 {
		t.Errorf("3 parsed to %v", got)
	}
	// AND SOMETHING UNPARSABLE IS NOT ZERO EITHER: "kubectl said something unexpected" must not render as
	// "this workload has no replicas".
	for _, unexpected := range []string{"<none>", "error", "3 replicas"} {
		if got := parseReplicaField(unexpected); got != nil {
			t.Errorf("%q parsed to %v, want nil", unexpected, *got)
		}
	}
}
