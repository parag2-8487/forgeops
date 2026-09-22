// SPDX-License-Identifier: Apache-2.0

// Logs and events: two read-only operations, one per system.
//
// Two rather than one because they are different systems with different failure modes — a container that
// was never started has no logs, a pod that was never scheduled has events explaining why and no logs at
// all — and one operation spanning both would have to report which half it could not answer. Read-only and
// approval-free, like the inventories.
//
// BOUNDED IN BOTH DIMENSIONS. `--tail` caps the lines and `--since` caps the window, because a container
// running for a month can hold gigabytes and an unbounded read would exhaust the agent's memory and then
// the browser's. The caps are reported alongside the output so a reader knows they are seeing a tail
// rather than the whole log — an unmarked truncation is how somebody concludes an error never happened.
package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

const (
	// MaxLogLines caps one read. 2000 is more than a human reads and small enough to cross a WebSocket
	// frame without chunking.
	MaxLogLines = 2000
	// DefaultLogLines is what a panel gets when it does not ask.
	DefaultLogLines = 200
	// MaxLogSinceSeconds caps the window at a day: older than that is an archive question, not a
	// dashboard one.
	MaxLogSinceSeconds = 24 * 60 * 60
)

type logArgs struct {
	// Container is a docker container name or id.
	Container string `json:"container,omitempty"`
	// Namespace, Pod and ContainerName address a Kubernetes pod.
	Namespace     string `json:"namespace,omitempty"`
	Pod           string `json:"pod,omitempty"`
	ContainerName string `json:"container_name,omitempty"`
	Context       string `json:"context,omitempty"`
	TailLines     int    `json:"tail_lines,omitempty"`
	SinceSeconds  int    `json:"since_seconds,omitempty"`
}

// LogReport is what both operations return.
type LogReport struct {
	Target string `json:"target"`
	// Lines in the order the tool emitted them.
	Lines []string `json:"lines"`
	// TailLines and SinceSeconds are the bounds that were APPLIED, which may differ from what was asked.
	TailLines    int `json:"tail_lines"`
	SinceSeconds int `json:"since_seconds"`
	// Truncated says whether the bound was reached, so a reader knows this is a tail. Without it an
	// operator can conclude an error never happened when it simply fell off the top.
	Truncated bool `json:"truncated"`
	// Events is populated for a pod only: a pod that never scheduled has no logs and its events are the
	// entire explanation.
	Events     []PodEvent `json:"events,omitempty"`
	ObservedAt string     `json:"observed_at"`
}

// PodEvent is one Kubernetes event about a pod.
type PodEvent struct {
	Type     string `json:"type"`
	Reason   string `json:"reason"`
	Message  string `json:"message"`
	Count    int    `json:"count"`
	LastSeen string `json:"last_seen"`
}

// clampLogBounds resolves the requested bounds against the caps.
func clampLogBounds(tail, since int) (int, int) {
	if tail <= 0 {
		tail = DefaultLogLines
	}
	if tail > MaxLogLines {
		tail = MaxLogLines
	}
	if since < 0 {
		since = 0
	}
	if since > MaxLogSinceSeconds {
		since = MaxLogSinceSeconds
	}
	return tail, since
}

func splitLines(output string) []string {
	lines := []string{}
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r\n", "\n"), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		lines = append(lines, line)
	}
	return lines
}

// dockerContainerLogs reads one container's recent output.
func dockerContainerLogs(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args logArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: container log arguments: %v", ErrBadArgs, err)
	}
	if strings.TrimSpace(args.Container) == "" {
		return Result{}, ErrNoTarget
	}
	runner, _, err := dockerRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	tail, since := clampLogBounds(args.TailLines, args.SinceSeconds)
	vector := []string{"logs", "--tail", strconv.Itoa(tail), "--timestamps"}
	if since > 0 {
		vector = append(vector, "--since", fmt.Sprintf("%ds", since))
	}
	vector = append(vector, args.Container)

	sink.Progress(50, string(OpDockerContainerLogs), "reading "+args.Container)
	outcome, runErr := runner.Run(ctx, "docker", vector...)
	if runErr != nil || !outcome.Passed {
		return Result{}, fmt.Errorf("executor: docker could not read %s's logs: %w — %s",
			args.Container, errOrRefused(runErr), firstLine(outcome.Output))
	}

	lines := splitLines(outcome.Output)
	report := LogReport{
		Target: args.Container, Lines: lines, TailLines: tail, SinceSeconds: since,
		Truncated: len(lines) >= tail, ObservedAt: d.now().UTC().Format(time.RFC3339),
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable log report: %w", err)
	}
	return Result{Status: "reported", Output: string(encoded)}, nil
}

// k8sPodDetail reads one pod's logs AND its events.
//
// Together because a pod that never scheduled has no logs and its events are the whole answer. Two
// operations would make a panel ask twice and then decide which absence to believe.
func k8sPodDetail(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args logArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: pod detail arguments: %v", ErrBadArgs, err)
	}
	if strings.TrimSpace(args.Pod) == "" || strings.TrimSpace(args.Namespace) == "" {
		return Result{}, fmt.Errorf("%w: a pod read needs both a namespace and a pod name", ErrNoTarget)
	}
	runner, base, _, err := kubectlRunner(ctx, d.root, args.Context)
	if err != nil {
		return Result{}, err
	}

	tail, since := clampLogBounds(args.TailLines, args.SinceSeconds)
	report := LogReport{
		Target:       args.Namespace + "/" + args.Pod,
		Lines:        []string{},
		Events:       []PodEvent{},
		TailLines:    tail,
		SinceSeconds: since,
		ObservedAt:   d.now().UTC().Format(time.RFC3339),
	}

	sink.Progress(40, string(OpKubernetesPodDetail), "reading logs for "+report.Target)
	logVector := append(append([]string{"logs"}, base...), args.Pod,
		"--namespace", args.Namespace, "--tail", strconv.Itoa(tail), "--timestamps")
	if since > 0 {
		logVector = append(logVector, fmt.Sprintf("--since=%ds", since))
	}
	if args.ContainerName != "" {
		logVector = append(logVector, "--container", args.ContainerName)
	}
	logs, logErr := runner.Run(ctx, "kubectl", logVector...)
	if logErr == nil && logs.Passed {
		report.Lines = splitLines(logs.Output)
		report.Truncated = len(report.Lines) >= tail
	} else {
		// NOT FATAL, and this is the reason the two reads are one operation: a pod in ImagePullBackOff has
		// no logs, and its events explain why. Refusing here would withhold the only useful half.
		report.Lines = []string{}
	}

	sink.Progress(70, string(OpKubernetesPodDetail), "reading events for "+report.Target)
	eventVector := append(append([]string{"get", "events"}, base...),
		"--namespace", args.Namespace,
		"--field-selector", "involvedObject.name="+args.Pod, "-o", "json")
	events, eventErr := runner.Run(ctx, "kubectl", eventVector...)
	if eventErr == nil && events.Passed {
		var payload struct {
			Items []struct {
				Type          string `json:"type"`
				Reason        string `json:"reason"`
				Message       string `json:"message"`
				Count         int    `json:"count"`
				LastTimestamp string `json:"lastTimestamp"`
			} `json:"items"`
		}
		if decodeErr := decodeFirstJSON(events.Output, &payload); decodeErr == nil {
			for _, item := range payload.Items {
				report.Events = append(report.Events, PodEvent{
					Type: item.Type, Reason: item.Reason, Message: item.Message,
					Count: item.Count, LastSeen: item.LastTimestamp,
				})
			}
		}
	}

	// BOTH HALVES EMPTY IS AN ERROR, because it means neither call worked and reporting it as "this pod is
	// quiet" would be a fabrication about a pod nobody could read.
	if len(report.Lines) == 0 && len(report.Events) == 0 && (logErr != nil || eventErr != nil) {
		return Result{}, fmt.Errorf("executor: neither logs nor events could be read for %s: %w",
			report.Target, errOrRefused(logErr))
	}

	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable pod detail: %w", err)
	}
	return Result{Status: "reported", Output: string(encoded)}, nil
}
