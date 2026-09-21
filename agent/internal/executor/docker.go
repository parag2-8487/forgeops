// SPDX-License-Identifier: Apache-2.0

// Docker: one read-only inventory operation and two mutating action operations.
//
// WHY THE DOCKER CLI AND NOT A GO ENGINE-API CLIENT. The deliverable says "Docker Engine API wrapper",
// and `docker` IS a client of that API; what is at stake is which client. A Go SDK would add a large
// dependency tree to a binary that ships to operators' machines, and — the deciding reason — it would
// need its own answer for where the daemon is, which socket, which TLS material and which context. The
// CLI already resolves all four from the operator's own configuration, which is the same property that
// makes `kubectl` the right tool for `deployment.apply_manifests`: the agent can only reach what the
// operator can already reach, and no envelope can widen that. Arguments are passed as a vector, never a
// command line, so there is still no shell anywhere on this path.
//
// WHY THREE OPERATIONS AND NOT FIFTEEN. `phases.md` §1.1 requires named operations, and a name must
// describe authority, not a verb. `docker.container_action` takes a closed set of actions over one named
// container; that is one authority — "may act on a container on this host" — and splitting it into five
// operations would multiply the whitelist without narrowing anything. What is NOT here is the thing a
// verb-per-operation split would hide: there is no operation that takes a docker command line, no
// `--privileged`, no bind-mount argument, and no `exec`. A container cannot be created with the host's
// filesystem attached by anything in this file.
//
// WHY THE INVENTORY IS READ-ONLY AND SEPARATE. A dashboard reads constantly and mutates rarely. Making
// the read an approval-free operation is what allows the panel to refresh without minting an approval per
// second; keeping it in a different operation from the actions is what keeps "look" from ever being a
// path to "change".
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

var (
	// ErrDockerMissing is reported as unimplemented-for-this-host rather than as a failure: nothing was
	// attempted, and the operator needs to install or start Docker, not debug this platform.
	ErrDockerMissing = errors.New("executor: docker is not on PATH, so no container can be inspected")
	// ErrDockerUnreachable separates "the tool is absent" from "the daemon is not answering". An
	// operator's remedy differs: install versus start.
	ErrDockerUnreachable = errors.New("executor: the docker daemon did not answer")
	// ErrUnknownContainerAction refuses an action outside the closed set by name.
	ErrUnknownContainerAction = errors.New("executor: that container action is not in the closed set")
	// ErrUnknownImageAction refuses an image action outside the closed set by name.
	ErrUnknownImageAction = errors.New("executor: that image action is not in the closed set")
	// ErrNoTarget refuses an action that names nothing rather than acting on everything. The
	// catastrophic default: `docker rm` with no argument is an error, but a wrapper that expanded an
	// empty name into "all" would be a host-wide delete from one malformed envelope.
	ErrNoTarget = errors.New("executor: the action names no target")
)

// containerActions is the closed set, mapping the action to the docker subcommand vector it becomes.
//
// `stop` and `restart` carry an explicit timeout so a container ignoring SIGTERM cannot hold the
// operation to its own budget. `remove` is NOT forced: `docker rm` on a running container fails, and that
// refusal is correct — stopping something that is still serving traffic is a decision for the operator,
// expressed by two actions, not an implicit `-f` this code chose for them.
var containerActions = map[string][]string{
	"start":   {"start"},
	"stop":    {"stop", "--time", "30"},
	"restart": {"restart", "--time", "30"},
	"remove":  {"rm"},
}

// imageActions is the closed set for images. `remove` is not forced, for the reason above: an image in
// use by a container fails to remove, and that is information.
var imageActions = map[string][]string{
	"pull":   {"pull"},
	"remove": {"rmi"},
}

// dockerInventoryArgs selects which families to report.
type dockerInventoryArgs struct {
	// Stats asks for a one-shot CPU/memory/network sample per running container. Off by default
	// because it costs a second of wall time per call and a list view does not always need it.
	Stats bool `json:"stats,omitempty"`
}

// DockerContainer is one container as the dashboard shows it.
type DockerContainer struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Image  string `json:"image"`
	State  string `json:"state"`
	Status string `json:"status"`
	Ports  string `json:"ports"`
	// CPUPercent and the memory figures are POINTERS, and that is the whole point of this struct.
	// `nil` means no sample was taken; 0 means a sample was taken and the container is idle. A
	// dashboard that rendered both as "0%" would show an idle container and an unmeasured one
	// identically, which is the defect class this phase was warned about.
	CPUPercent  *float64 `json:"cpu_percent"`
	MemoryBytes *float64 `json:"memory_bytes"`
	MemoryLimit *float64 `json:"memory_limit_bytes"`
	NetworkRx   *float64 `json:"network_rx_bytes"`
	NetworkTx   *float64 `json:"network_tx_bytes"`
}

// DockerImage is one image.
type DockerImage struct {
	ID         string `json:"id"`
	Repository string `json:"repository"`
	Tag        string `json:"tag"`
	Size       string `json:"size"`
	CreatedAt  string `json:"created_at"`
}

// DockerNamed is a volume or a network, which the dashboard lists by name and driver only.
type DockerNamed struct {
	Name   string `json:"name"`
	Driver string `json:"driver"`
}

// DockerInventory is the whole read.
type DockerInventory struct {
	Containers []DockerContainer `json:"containers"`
	Images     []DockerImage     `json:"images"`
	Volumes    []DockerNamed     `json:"volumes"`
	Networks   []DockerNamed     `json:"networks"`
	// DockerVersion answers "which daemon reported this".
	DockerVersion string `json:"docker_version"`
	// ObservedAt is when the sample was taken, in RFC 3339. The backend needs it to decide whether a
	// panel is showing something current or something stale, and it must come from the host that took
	// the reading rather than from the time the browser rendered it.
	ObservedAt string `json:"observed_at"`
	// StatsSampled says whether the CPU and memory columns were populated at all. Without it, a
	// dashboard cannot distinguish "stats were not requested" from "every container is idle".
	StatsSampled bool `json:"stats_sampled"`
}

// dockerContainerActionArgs is one action over one named container.
type dockerContainerActionArgs struct {
	Action string `json:"action"`
	// Container is a name or an ID. Never a pattern: a glob would let one envelope act on an unbounded
	// set, which is the blast radius the change-set machinery exists to bound.
	Container string `json:"container"`
}

// dockerImageActionArgs is one action over one named image reference.
type dockerImageActionArgs struct {
	Action string `json:"action"`
	Image  string `json:"image"`
}

// DockerActionReport is what a mutating docker operation reports.
type DockerActionReport struct {
	Action string `json:"action"`
	Target string `json:"target"`
	// StateBefore and StateAfter are read from the daemon around the action, so the audit row records
	// what changed rather than what was requested. A restart of an already-stopped container and a
	// restart of a running one are different events and the difference is only visible here.
	StateBefore string `json:"state_before"`
	StateAfter  string `json:"state_after"`
	Output      string `json:"output"`
	ObservedAt  string `json:"observed_at"`
}

// dockerRunner builds the runner and refuses early when Docker is absent or silent.
//
// The daemon check is a real call, not a `LookPath`: `docker` on PATH with no daemon behind it is the
// commonest state on a developer machine, and letting the first real subcommand fail would report
// "could not list containers" for what is actually "Docker Desktop is not running".
func dockerRunner(ctx context.Context, dir string) (*validator.Runner, string, error) {
	runner := &validator.Runner{Dir: dir}
	if _, err := runner.Look("docker"); err != nil {
		return nil, "", fmt.Errorf("%w: %w", ErrDockerMissing, err)
	}
	outcome, err := runner.Run(ctx, "docker", "version", "--format", "{{.Server.Version}}")
	if err != nil || !outcome.Passed {
		return nil, "", fmt.Errorf("%w: %s", ErrDockerUnreachable, firstLine(outcome.Output))
	}
	return runner, strings.TrimSpace(outcome.Output), nil
}

// decodeJSONLines reads docker's `--format json` output, which is ONE JSON OBJECT PER LINE and not a JSON
// array. Unmarshalling the whole buffer fails on the second line, and a wrapper that treated that failure
// as "no containers" would report an empty host.
func decodeJSONLines(output string, each func(json.RawMessage) error) error {
	for _, line := range strings.Split(output, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		if err := each(json.RawMessage(trimmed)); err != nil {
			return fmt.Errorf("decoding %q: %w", firstLine(trimmed), err)
		}
	}
	return nil
}

// parseStatPercent turns docker's "12.34%" into a number. A value that cannot be parsed yields nil
// rather than zero: "the daemon said something this code did not understand" must not render as "idle".
func parseStatPercent(raw string) *float64 {
	trimmed := strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(raw), "%"))
	if trimmed == "" {
		return nil
	}
	var value float64
	if _, err := fmt.Sscanf(trimmed, "%g", &value); err != nil {
		return nil
	}
	return &value
}

// parseStatBytes turns docker's "1.5MiB / 2GiB" style pair into bytes. Returns nil for either side it
// cannot read, for the same reason as above.
func parseStatBytes(raw string) (used, limit *float64) {
	parts := strings.Split(raw, "/")
	if len(parts) != 2 {
		return nil, nil
	}
	return parseByteSize(parts[0]), parseByteSize(parts[1])
}

var byteUnits = []struct {
	suffix string
	scale  float64
}{
	{"GiB", 1024 * 1024 * 1024},
	{"MiB", 1024 * 1024},
	{"KiB", 1024},
	{"GB", 1000 * 1000 * 1000},
	{"MB", 1000 * 1000},
	{"kB", 1000},
	{"B", 1},
}

func parseByteSize(raw string) *float64 {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return nil
	}
	for _, unit := range byteUnits {
		if !strings.HasSuffix(trimmed, unit.suffix) {
			continue
		}
		var value float64
		if _, err := fmt.Sscanf(strings.TrimSpace(strings.TrimSuffix(trimmed, unit.suffix)), "%g", &value); err != nil {
			return nil
		}
		scaled := value * unit.scale
		return &scaled
	}
	return nil
}

// dockerInventory is the read-only handler.
func dockerInventory(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args dockerInventoryArgs
	if len(v.Args()) > 0 {
		if err := json.Unmarshal(v.Args(), &args); err != nil {
			return Result{}, fmt.Errorf("%w: docker inventory arguments: %v", ErrBadArgs, err)
		}
	}

	runner, version, err := dockerRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	sink.Progress(20, string(OpDockerInventory), "listing containers, images, volumes and networks")

	inventory := DockerInventory{
		DockerVersion: version,
		ObservedAt:    d.now().UTC().Format(time.RFC3339),
		Containers:    []DockerContainer{},
		Images:        []DockerImage{},
		Volumes:       []DockerNamed{},
		Networks:      []DockerNamed{},
	}

	// `--all`, because a dashboard that hid stopped containers would answer "where did it go" with
	// silence. The state column distinguishes them.
	containers, err := runner.Run(ctx, "docker", "ps", "--all", "--format", "json")
	if err != nil || !containers.Passed {
		return Result{}, fmt.Errorf("executor: docker could not list containers: %s", firstLine(containers.Output))
	}
	if err := decodeJSONLines(containers.Output, func(raw json.RawMessage) error {
		var row struct {
			ID     string `json:"ID"`
			Names  string `json:"Names"`
			Image  string `json:"Image"`
			State  string `json:"State"`
			Status string `json:"Status"`
			Ports  string `json:"Ports"`
		}
		if err := json.Unmarshal(raw, &row); err != nil {
			return err
		}
		inventory.Containers = append(inventory.Containers, DockerContainer{
			ID: row.ID, Name: row.Names, Image: row.Image,
			State: row.State, Status: row.Status, Ports: row.Ports,
		})
		return nil
	}); err != nil {
		return Result{}, fmt.Errorf("executor: docker's container list was unreadable: %w", err)
	}

	images, err := runner.Run(ctx, "docker", "images", "--format", "json")
	if err != nil || !images.Passed {
		return Result{}, fmt.Errorf("executor: docker could not list images: %s", firstLine(images.Output))
	}
	if err := decodeJSONLines(images.Output, func(raw json.RawMessage) error {
		var row struct {
			ID         string `json:"ID"`
			Repository string `json:"Repository"`
			Tag        string `json:"Tag"`
			Size       string `json:"Size"`
			CreatedAt  string `json:"CreatedAt"`
		}
		if err := json.Unmarshal(raw, &row); err != nil {
			return err
		}
		inventory.Images = append(inventory.Images, DockerImage{
			ID: row.ID, Repository: row.Repository, Tag: row.Tag,
			Size: row.Size, CreatedAt: row.CreatedAt,
		})
		return nil
	}); err != nil {
		return Result{}, fmt.Errorf("executor: docker's image list was unreadable: %w", err)
	}

	volumes, err := runner.Run(ctx, "docker", "volume", "ls", "--format", "json")
	if err != nil || !volumes.Passed {
		return Result{}, fmt.Errorf("executor: docker could not list volumes: %s", firstLine(volumes.Output))
	}
	if err := decodeJSONLines(volumes.Output, func(raw json.RawMessage) error {
		var row struct {
			Name   string `json:"Name"`
			Driver string `json:"Driver"`
		}
		if err := json.Unmarshal(raw, &row); err != nil {
			return err
		}
		inventory.Volumes = append(inventory.Volumes, DockerNamed{Name: row.Name, Driver: row.Driver})
		return nil
	}); err != nil {
		return Result{}, fmt.Errorf("executor: docker's volume list was unreadable: %w", err)
	}

	networks, err := runner.Run(ctx, "docker", "network", "ls", "--format", "json")
	if err != nil || !networks.Passed {
		return Result{}, fmt.Errorf("executor: docker could not list networks: %s", firstLine(networks.Output))
	}
	if err := decodeJSONLines(networks.Output, func(raw json.RawMessage) error {
		var row struct {
			Name   string `json:"Name"`
			Driver string `json:"Driver"`
		}
		if err := json.Unmarshal(raw, &row); err != nil {
			return err
		}
		inventory.Networks = append(inventory.Networks, DockerNamed{Name: row.Name, Driver: row.Driver})
		return nil
	}); err != nil {
		return Result{}, fmt.Errorf("executor: docker's network list was unreadable: %w", err)
	}

	if args.Stats {
		sink.Progress(70, string(OpDockerInventory), "sampling cpu, memory and network")
		// `--no-stream` takes one sample and exits. Without it this blocks forever, which inside an
		// operation budget would surface as a timeout rather than as a reading.
		stats, statsErr := runner.Run(ctx, "docker", "stats", "--no-stream", "--format", "json")
		if statsErr == nil && stats.Passed {
			byName := map[string]*DockerContainer{}
			for index := range inventory.Containers {
				byName[inventory.Containers[index].Name] = &inventory.Containers[index]
				byName[inventory.Containers[index].ID] = &inventory.Containers[index]
			}
			// The flag is set only if the sample actually parsed into at least one container, so a
			// `docker stats` that returned nothing usable does not claim to have measured anything.
			if err := decodeJSONLines(stats.Output, func(raw json.RawMessage) error {
				var row struct {
					Name     string `json:"Name"`
					ID       string `json:"ID"`
					CPUPerc  string `json:"CPUPerc"`
					MemUsage string `json:"MemUsage"`
					NetIO    string `json:"NetIO"`
				}
				if err := json.Unmarshal(raw, &row); err != nil {
					return err
				}
				target := byName[row.Name]
				if target == nil {
					target = byName[row.ID]
				}
				if target == nil {
					return nil
				}
				target.CPUPercent = parseStatPercent(row.CPUPerc)
				target.MemoryBytes, target.MemoryLimit = parseStatBytes(row.MemUsage)
				target.NetworkRx, target.NetworkTx = parseStatBytes(row.NetIO)
				inventory.StatsSampled = true
				return nil
			}); err != nil {
				// A stats line this code cannot read leaves every figure nil and SAYS SO through the
				// flag. Reporting an error for the whole inventory would hide a perfectly good
				// container list because one optional column was unavailable.
				inventory.StatsSampled = false
			}
		}
	}

	encoded, err := json.Marshal(inventory)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable docker inventory: %w", err)
	}
	sink.Progress(100, string(OpDockerInventory), fmt.Sprintf("%d container(s), %d image(s)",
		len(inventory.Containers), len(inventory.Images)))
	return Result{Status: "reported", Output: string(encoded)}, nil
}

// containerState reads one container's state, or "" when it cannot be read. Used either side of an
// action so the report names what actually changed.
func containerState(ctx context.Context, runner *validator.Runner, name string) string {
	outcome, err := runner.Run(ctx, "docker", "inspect", "--format", "{{.State.Status}}", name)
	if err != nil || !outcome.Passed {
		return ""
	}
	return strings.TrimSpace(outcome.Output)
}

// dockerContainerAction is the mutating handler for containers.
func dockerContainerAction(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args dockerContainerActionArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: docker container action arguments: %v", ErrBadArgs, err)
	}
	if strings.TrimSpace(args.Container) == "" {
		return Result{}, ErrNoTarget
	}
	subcommand, known := containerActions[args.Action]
	if !known {
		return Result{}, fmt.Errorf("%w: %q", ErrUnknownContainerAction, args.Action)
	}

	runner, _, err := dockerRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	before := containerState(ctx, runner, args.Container)
	// A target that does not exist is refused BEFORE the action, so "there is no such container" does
	// not arrive as a generic docker failure.
	if before == "" {
		return Result{}, fmt.Errorf("%w: docker does not know a container named %q",
			ErrNoTarget, args.Container)
	}

	sink.Progress(40, string(OpDockerContainerAction), fmt.Sprintf("%s %s", args.Action, args.Container))
	outcome, runErr := runner.Run(ctx, "docker", append(subcommand, args.Container)...)
	if runErr != nil || !outcome.Passed {
		return Result{}, fmt.Errorf("executor: docker %s refused %s: %w — %s",
			args.Action, args.Container, errOrRefused(runErr), firstLine(outcome.Output))
	}

	after := containerState(ctx, runner, args.Container)
	if args.Action == "remove" && after == "" {
		// The one case where an unreadable state is the SUCCESS: a removed container has no state.
		after = "removed"
	}

	report := DockerActionReport{
		Action: args.Action, Target: args.Container,
		StateBefore: before, StateAfter: after,
		Output: strings.TrimSpace(outcome.Output), ObservedAt: d.now().UTC().Format(time.RFC3339),
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable docker action report: %w", err)
	}
	sink.Progress(100, string(OpDockerContainerAction),
		fmt.Sprintf("%s is now %s", args.Container, after))
	return Result{Status: "applied", Output: string(encoded)}, nil
}

// dockerImageAction is the mutating handler for images.
func dockerImageAction(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args dockerImageActionArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("%w: docker image action arguments: %v", ErrBadArgs, err)
	}
	if strings.TrimSpace(args.Image) == "" {
		return Result{}, ErrNoTarget
	}
	subcommand, known := imageActions[args.Action]
	if !known {
		return Result{}, fmt.Errorf("%w: %q", ErrUnknownImageAction, args.Action)
	}

	runner, _, err := dockerRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	sink.Progress(40, string(OpDockerImageAction), fmt.Sprintf("%s %s", args.Action, args.Image))
	outcome, runErr := runner.Run(ctx, "docker", append(subcommand, args.Image)...)
	if runErr != nil || !outcome.Passed {
		return Result{}, fmt.Errorf("executor: docker %s refused %s: %w — %s",
			args.Action, args.Image, errOrRefused(runErr), firstLine(outcome.Output))
	}

	report := DockerActionReport{
		Action: args.Action, Target: args.Image,
		Output: strings.TrimSpace(outcome.Output), ObservedAt: d.now().UTC().Format(time.RFC3339),
	}
	// For a pull, the DIGEST is the fact worth keeping: a tag moves and a digest does not, so a
	// deployment record that pins the digest can be reproduced and one that pins the tag cannot.
	if args.Action == "pull" {
		if digest := imageDigest(ctx, runner, args.Image); digest != "" {
			report.StateAfter = digest
		}
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable docker action report: %w", err)
	}
	sink.Progress(100, string(OpDockerImageAction), fmt.Sprintf("%s %s", args.Action, args.Image))
	return Result{Status: "applied", Output: string(encoded)}, nil
}

// imageDigest reads a local image's repo digest. Empty when the image has none — an image built locally
// and never pushed genuinely has no digest, and inventing one would be worse than reporting absence.
func imageDigest(ctx context.Context, runner *validator.Runner, reference string) string {
	outcome, err := runner.Run(ctx, "docker", "inspect", "--format", "{{index .RepoDigests 0}}", reference)
	if err != nil || !outcome.Passed {
		return ""
	}
	digest := strings.TrimSpace(outcome.Output)
	if digest == "" || strings.Contains(digest, "no value") {
		return ""
	}
	return digest
}
