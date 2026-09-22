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
//
// PHASE 1 EXPOSED NO `Apply` AND PHASE 2 ADDS ONE (design 1.4, 14.6, phases 2.2). The earlier absence
// was not squeamishness about the verb: there was nothing to hold the state lock, no approval to
// attach an apply to, and no saved plan to apply, so an `Apply` then would have re-planned at run time
// and applied whatever it found. All three now exist, and `Apply` takes a SAVED PLAN FILE and no
// `Lock` option at all, so the two properties that made the verb dangerous are structural rather than
// left to a caller.
type Runner interface {
	Validate(ctx context.Context, workdir string) (*ValidateResult, error)
	Plan(ctx context.Context, workdir string, opts PlanOptions) (*PlanResult, error)
	Apply(ctx context.Context, workdir string, opts ApplyOptions) (*ApplyResult, error)
}

// ErrTofuNotFound is returned when the configured tofu binary cannot be located.
var ErrTofuNotFound = errors.New("tofu binary not found")
