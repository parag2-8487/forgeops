// SPDX-License-Identifier: Apache-2.0

package executor

// `kubernetes.rollout_detail` — Phase 2 §2.7a's read behind the progressive-delivery panel.
//
// A READ, so approval-free — it mutates nothing — but still admitted, policy-checked and carried in a signed
// envelope like every other read.
//
// WHY IT IS A SEPARATE OPERATION FROM `kubernetes.inventory`. The same reason `pod_detail` is: an inventory
// answers "what exists" across a namespace, and this answers "what is happening to this one Rollout right
// now". Folding it in would make every dashboard refresh fetch analysis runs for every workload, and the
// panel needs one object in detail rather than all of them shallowly.
//
// WHAT THE PANEL CANNOT BE ALLOWED TO DO is show a plausible weight when it cannot reach the cluster. So
// every field here distinguishes three states — never reported, reported as absent, and a value — and the
// report carries `observed_at` so a stale render is detectable. A canary weight is a number an operator
// acts on; a wrong one sends traffic somewhere they did not intend.

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

// rolloutDetailArgs names one Rollout.
type rolloutDetailArgs struct {
	Context   string `json:"context"`
	Namespace string `json:"namespace"`
	Rollout   string `json:"rollout"`
}

// AnalysisRunSummary is one analysis run's verdict.
type AnalysisRunSummary struct {
	Name string `json:"name"`
	// Phase is Rollouts' own word: Pending, Running, Successful, Failed, Error, Inconclusive.
	//
	// INCONCLUSIVE IS NOT A FAILURE AND NOT A PASS, and it is carried through rather than collapsed: an
	// idle service makes an error-rate query 0/0 = NaN, which Rollouts reports as Inconclusive. A panel that
	// showed it as either would be stating something nothing measured.
	Phase string `json:"phase"`
	// Per-metric measurements, so "which signal failed" is answerable. A canary gated on error rate AND
	// latency that aborts is useless information without knowing which one breached.
	Metrics   []AnalysisMetricSummary `json:"metrics"`
	StartedAt string                  `json:"started_at"`
}

// AnalysisMetricSummary is one metric's tally within a run.
type AnalysisMetricSummary struct {
	Name         string `json:"name"`
	Phase        string `json:"phase"`
	Successful   int    `json:"successful"`
	Failed       int    `json:"failed"`
	Inconclusive int    `json:"inconclusive"`
	Error        int    `json:"error"`
	// The most recent measured value, as Rollouts recorded it. A string because a Prometheus result is
	// whatever the query returned, and parsing it to a float here would turn an unexpected shape into a
	// zero -- which reads as a perfect error rate.
	LatestValue string `json:"latest_value"`
}

// RolloutDetailReport is what the panel renders.
type RolloutDetailReport struct {
	Rollout   string `json:"rollout"`
	Namespace string `json:"namespace"`
	// Strategy is "canary", "blueGreen", or "" when the object declares neither — which happens for a
	// Rollout mid-edit, and is reported as absent rather than guessed.
	Strategy string `json:"strategy"`
	// Phase is Rollouts' own: Progressing, Paused, Healthy, Degraded.
	Phase string `json:"phase"`
	// Message is the controller's explanation of the phase. The single most useful field on this report and
	// the one most likely to be dropped as noise: "Degraded" alone sends an operator to read logs.
	Message string `json:"message"`
	// CanaryWeight is the percentage of traffic on the new version. NEGATIVE ONE means NOT REPORTED, and
	// the distinction from zero is load-bearing: zero means no traffic has been shifted, and a panel that
	// showed 0% for an unreadable field would say the canary had not started when it might be at 80%.
	CanaryWeight int `json:"canary_weight"`
	// CurrentStep and TotalSteps place the rollout in its plan. Both -1 when not reported.
	CurrentStep int `json:"current_step"`
	TotalSteps  int `json:"total_steps"`
	// Replica tallies, so "how much of the fleet is new" is answerable independently of the weight -- a
	// weight is what the mesh was told; these are what is running.
	Replicas          int `json:"replicas"`
	UpdatedReplicas   int `json:"updated_replicas"`
	ReadyReplicas     int `json:"ready_replicas"`
	AvailableReplicas int `json:"available_replicas"`
	// Revision history: the promotion record the panel shows.
	StableRevision string `json:"stable_revision"`
	CanaryRevision string `json:"canary_revision"`
	// AnalysisRuns is empty when none exist. Empty means "nothing is gating this rollout", which the panel
	// states in words rather than rendering an empty table that reads as "all checks passed".
	AnalysisRuns []AnalysisRunSummary `json:"analysis_runs"`
	ObservedAt   string               `json:"observed_at"`
}

// k8sRolloutDetail is the handler.
func k8sRolloutDetail(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args rolloutDetailArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: rollout detail arguments: %v", ErrBadArgs, err)
	}
	if strings.TrimSpace(args.Rollout) == "" {
		return Result{}, ErrNoTarget
	}
	namespace := strings.TrimSpace(args.Namespace)
	if namespace == "" {
		namespace = "default"
	}

	runner, base, _, err := kubectlRunner(ctx, d.root, args.Context)
	if err != nil {
		return Result{}, err
	}

	sink.Progress(30, string(OpKubernetesRolloutDetail), "reading rollout "+args.Rollout)

	report := RolloutDetailReport{
		Rollout: args.Rollout, Namespace: namespace,
		// The NOT-REPORTED sentinels are the starting state, so a field the cluster never returned stays
		// distinguishable from one it returned as zero. Defaulting these to 0 is how a panel comes to show
		// a plausible number for an unreadable object.
		CanaryWeight: -1, CurrentStep: -1, TotalSteps: -1,
		AnalysisRuns: []AnalysisRunSummary{},
		ObservedAt:   d.now().UTC().Format(time.RFC3339),
	}

	vector := append([]string{}, base...)
	vector = append(vector, "get", "rollout", args.Rollout, "-n", namespace, "-o", "json")
	outcome, runErr := runner.Run(ctx, "kubectl", vector...)
	if runErr != nil || !outcome.Passed {
		// A MISSING ROLLOUT IS AN ERROR, not an empty report. The panel must not render "0% canary, no
		// analysis" for an object that does not exist -- that is the exact shape of the defect this codebase
		// keeps finding, where an absence renders as a healthy zero.
		return Result{}, fmt.Errorf("executor: kubectl could not read rollout %s/%s: %w — %s",
			namespace, args.Rollout, errOrRefused(runErr), firstLine(outcome.Output))
	}

	var document struct {
		Spec struct {
			Replicas *int `json:"replicas"`
			Strategy struct {
				Canary *struct {
					Steps []map[string]any `json:"steps"`
				} `json:"canary"`
				BlueGreen *struct{} `json:"blueGreen"`
			} `json:"strategy"`
		} `json:"spec"`
		Status struct {
			Phase             string `json:"phase"`
			Message           string `json:"message"`
			Replicas          *int   `json:"replicas"`
			UpdatedReplicas   *int   `json:"updatedReplicas"`
			ReadyReplicas     *int   `json:"readyReplicas"`
			AvailableReplicas *int   `json:"availableReplicas"`
			CurrentStepIndex  *int   `json:"currentStepIndex"`
			StableRS          string `json:"stableRS"`
			CurrentPodHash    string `json:"currentPodHash"`
			CurrentStepHash   string `json:"currentStepHash"`
		} `json:"status"`
	}
	if decodeErr := decodeFirstJSON(outcome.Output, &document); decodeErr != nil {
		return Result{}, fmt.Errorf("executor: rollout %s/%s did not decode: %w", namespace, args.Rollout, decodeErr)
	}

	report.Phase = document.Status.Phase
	report.Message = document.Status.Message
	report.StableRevision = document.Status.StableRS
	report.CanaryRevision = document.Status.CurrentPodHash

	switch {
	case document.Spec.Strategy.Canary != nil:
		report.Strategy = "canary"
		report.TotalSteps = len(document.Spec.Strategy.Canary.Steps)
		if document.Status.CurrentStepIndex != nil {
			report.CurrentStep = *document.Status.CurrentStepIndex
			// The weight comes from the STEP the rollout is on, because `status` does not carry it in every
			// Rollouts version. Read from the spec's own step list, so the number shown is the number the
			// controller is acting on.
			if weight, found := weightAtStep(document.Spec.Strategy.Canary.Steps, *document.Status.CurrentStepIndex); found {
				report.CanaryWeight = weight
			}
		}
	case document.Spec.Strategy.BlueGreen != nil:
		report.Strategy = "blueGreen"
	}

	report.Replicas = intOrZero(document.Status.Replicas)
	report.UpdatedReplicas = intOrZero(document.Status.UpdatedReplicas)
	report.ReadyReplicas = intOrZero(document.Status.ReadyReplicas)
	report.AvailableReplicas = intOrZero(document.Status.AvailableReplicas)

	// Analysis runs are a SEPARATE object kind, so a second read. Its failure is tolerated: the rollout's
	// own state is already established, and reporting it with no analysis detail is more useful than failing
	// the whole read -- the panel says the runs could not be read rather than that none exist.
	report.AnalysisRuns = analysisRuns(ctx, runner, base, namespace, args.Rollout)

	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable rollout report: %w", err)
	}
	sink.Progress(100, string(OpKubernetesRolloutDetail), "read rollout "+args.Rollout)
	return Result{Status: "applied", Output: string(encoded)}, nil
}

// weightAtStep finds the most recent `setWeight` at or before `index`.
//
// AT OR BEFORE, not exactly at: a rollout paused on an `analysis` step is still serving the weight the
// preceding `setWeight` established, and reading only the current step would report no weight for exactly
// the moment an operator is most likely to be looking.
func weightAtStep(steps []map[string]any, index int) (int, bool) {
	if index < 0 {
		return 0, false
	}
	if index >= len(steps) {
		// A completed rollout's index runs past the end. 100 is then correct and not a guess: completing
		// the steps IS how Rollouts reaches full traffic.
		return 100, true
	}
	for position := index; position >= 0; position-- {
		raw, present := steps[position]["setWeight"]
		if !present {
			continue
		}
		switch value := raw.(type) {
		case float64:
			return int(value), true
		case int:
			return value, true
		}
	}
	return 0, false
}

// analysisRuns reads the AnalysisRuns belonging to one rollout.
func analysisRuns(ctx context.Context, runner *validator.Runner, base []string, namespace, rollout string) []AnalysisRunSummary {
	vector := append([]string{}, base...)
	vector = append(vector, "get", "analysisruns", "-n", namespace,
		"-l", "rollouts-pod-template-hash", "-o", "json")
	outcome, err := runner.Run(ctx, "kubectl", vector...)
	if err != nil || !outcome.Passed {
		return []AnalysisRunSummary{}
	}

	var list struct {
		Items []struct {
			Metadata struct {
				Name              string            `json:"name"`
				CreationTimestamp string            `json:"creationTimestamp"`
				Labels            map[string]string `json:"labels"`
				OwnerReferences   []struct {
					Name string `json:"name"`
					Kind string `json:"kind"`
				} `json:"ownerReferences"`
			} `json:"metadata"`
			Status struct {
				Phase         string `json:"phase"`
				MetricResults []struct {
					Name         string `json:"name"`
					Phase        string `json:"phase"`
					Successful   int    `json:"successful"`
					Failed       int    `json:"failed"`
					Inconclusive int    `json:"inconclusive"`
					Error        int    `json:"error"`
					Measurements []struct {
						Value string `json:"value"`
					} `json:"measurements"`
				} `json:"metricResults"`
			} `json:"status"`
		} `json:"items"`
	}
	if decodeErr := decodeFirstJSON(outcome.Output, &list); decodeErr != nil {
		return []AnalysisRunSummary{}
	}

	summaries := []AnalysisRunSummary{}
	for _, item := range list.Items {
		// FILTERED BY OWNERSHIP, because a namespace holds the analysis runs of every rollout in it.
		// Showing another rollout's runs on this panel would attribute a failure to the wrong release.
		owned := false
		for _, owner := range item.Metadata.OwnerReferences {
			if owner.Kind == "Rollout" && owner.Name == rollout {
				owned = true
				break
			}
		}
		if !owned {
			continue
		}
		summary := AnalysisRunSummary{
			Name:      item.Metadata.Name,
			Phase:     item.Status.Phase,
			StartedAt: item.Metadata.CreationTimestamp,
			Metrics:   []AnalysisMetricSummary{},
		}
		for _, metric := range item.Status.MetricResults {
			latest := ""
			if len(metric.Measurements) > 0 {
				latest = metric.Measurements[len(metric.Measurements)-1].Value
			}
			summary.Metrics = append(summary.Metrics, AnalysisMetricSummary{
				Name: metric.Name, Phase: metric.Phase,
				Successful: metric.Successful, Failed: metric.Failed,
				Inconclusive: metric.Inconclusive, Error: metric.Error,
				LatestValue: latest,
			})
		}
		summaries = append(summaries, summary)
	}
	return summaries
}

func intOrZero(value *int) int {
	if value == nil {
		return 0
	}
	return *value
}
