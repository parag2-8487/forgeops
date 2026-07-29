// SPDX-License-Identifier: Apache-2.0
package iac

import (
	"context"
	"encoding/json"
	"errors"
	"time"
)

// TofuConfig holds configuration for the OpenTofu runner.
type TofuConfig struct {
	BinaryPath     string        // default "tofu"
	DefaultTimeout time.Duration // default 5m
	KillGrace      time.Duration // default 10s
	PluginCacheDir string        // TF_PLUGIN_CACHE_DIR
	ExtraEnvAllow  []string      // additional env keys permitted through
	MaxLineBytes   int           // default 64KiB
}

// DefaultTofuConfig returns a TofuConfig with sensible defaults.
func DefaultTofuConfig() TofuConfig {
	return TofuConfig{
		BinaryPath:     "tofu",
		DefaultTimeout: 5 * time.Minute,
		KillGrace:      10 * time.Second,
		MaxLineBytes:   64 * 1024, // 64 KiB
	}
}

// ValidateResult holds the outcome of a tofu validate invocation.
type ValidateResult struct {
	ExitCode    int
	Diagnostics json.RawMessage // tofu validate -json output
	Stdout      []string
	Stderr      []string
	Duration    time.Duration
}

// PlanOptions configures a tofu plan invocation.
type PlanOptions struct {
	VarFiles []string
	Vars     map[string]string
	Target   []string
	Lock     bool
}

// PlanResult holds the outcome of a tofu plan invocation.
type PlanResult struct {
	ExitCode   int
	HasChanges bool            // exit code 2 with -detailed-exitcode
	PlanJSON   json.RawMessage // from tofu show -json <planfile>
	Stdout     []string
	Stderr     []string
	Duration   time.Duration
}

// LineSink receives streaming output lines. stream is "stdout" or "stderr".
type LineSink func(stream string, line string)

// Runner defines the contract for OpenTofu operations.
// No Apply is exposed — by design.
type Runner interface {
	Validate(ctx context.Context, workdir string) (*ValidateResult, error)
	Plan(ctx context.Context, workdir string, opts PlanOptions) (*PlanResult, error)
}

// ErrTofuNotFound is returned when the configured tofu binary cannot be located.
var ErrTofuNotFound = errors.New("tofu binary not found")
