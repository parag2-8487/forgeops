package models

// WorkloadType classifies the target application workload.
type WorkloadType string

const (
	WorkloadTypeWebService       WorkloadType = "web_service"
	WorkloadTypeTCPService       WorkloadType = "tcp_service"
	WorkloadTypeBackgroundWorker WorkloadType = "background_worker"
	WorkloadTypeBatchJob         WorkloadType = "batch_job"
	WorkloadTypeStaticSPA        WorkloadType = "static_spa"
)

// ProtocolType defines the network protocol.
type ProtocolType string

const (
	ProtocolTypeHTTP ProtocolType = "http"
	ProtocolTypeTCP  ProtocolType = "tcp"
	ProtocolTypeNone ProtocolType = "none"
)

// NetworkContract contains port exposure and health check specifications.
type NetworkContract struct {
	ListenPort       *int         `json:"listen_port"`
	Protocol         ProtocolType `json:"protocol"`
	HealthCheckPath  string       `json:"health_check_path"`
	ExposedEndpoints []string     `json:"exposed_endpoints"`
}

// BuildConfig encapsulates build commands and output paths.
type BuildConfig struct {
	SourceDir         string   `json:"source_dir"`
	BuildCommand      string   `json:"build_command"`
	ArtifactOutputDir string   `json:"artifact_output_dir"`
	InstallCommand    string   `json:"install_command"`
	CacheDirs         []string `json:"cache_dirs"`
}

// RuntimeContract encapsulates runtime engine, framework, and start command.
type RuntimeContract struct {
	Language             string            `json:"language"`
	RuntimeVersion       string            `json:"runtime_version"`
	Framework            string            `json:"framework"`
	PackageManager       string            `json:"package_manager"`
	StartCommand         string            `json:"start_command"`
	EnvironmentVariables map[string]string `json:"environment_variables"`
}

// AmbiguityResolution tracks detection confidence and operator intervention.
type AmbiguityResolution struct {
	IsAmbiguous        bool     `json:"is_ambiguous"`
	ResolutionStrategy string   `json:"resolution_strategy"`
	ConfidenceScore    float64  `json:"confidence_score"`
	DetectedCandidates []string `json:"detected_candidates"`
	UnresolvedReason   string   `json:"unresolved_reason"`
}

// ProjectBlueprint is the authoritative blueprint contract received from backend.
type ProjectBlueprint struct {
	BlueprintID    string              `json:"blueprint_id"`
	RepositoryRoot string              `json:"repository_root"`
	IsMonorepo     bool                `json:"is_monorepo"`
	WorkloadType   WorkloadType        `json:"workload_type"`
	BuildConfig    BuildConfig         `json:"build_config"`
	Runtime        RuntimeContract     `json:"runtime"`
	Network        NetworkContract     `json:"network"`
	Ambiguity      AmbiguityResolution `json:"ambiguity"`
}

// DiagnosticBundle aggregates all forensic data when any verification gate fails.
type DiagnosticBundle struct {
	GateIdentifier     string            `json:"gate_identifier"`
	Stage              string            `json:"stage"`
	Blueprint          ProjectBlueprint  `json:"blueprint"`
	DeploymentManifest map[string]string `json:"deployment_manifest"`
	CommandLine        string            `json:"command_line"`
	ExitCode           int               `json:"exit_code"`
	AttemptCount       int               `json:"attempt_count"`
	Stdout             string            `json:"stdout"`
	Stderr             string            `json:"stderr"`
	ErrorClassification string           `json:"error_classification"`
	ContainerState     map[string]any    `json:"container_state"`
	ContainerLogs      string            `json:"container_logs"`
	TreeSnippet        string            `json:"tree_snippet"`
}
