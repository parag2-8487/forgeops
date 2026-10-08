// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"regexp"
	"strings"
)

// ErrorClass defines the categorization of an execution error.
type ErrorClass string

const (
	ErrorClassDeterministic   ErrorClass = "DETERMINISTIC"
	ErrorClassTransient       ErrorClass = "TRANSIENT"
	ErrorClassApplicationCode ErrorClass = "APPLICATION_CODE"
	ErrorClassEngineInternal  ErrorClass = "ENGINE_INTERNAL"
)

// ExecutionError encapsulates classified error information with actionable diagnostics.
type ExecutionError struct {
	Class      ErrorClass `json:"class"`
	Stage      string     `json:"stage"`
	ExitCode   int        `json:"exit_code"`
	Message    string     `json:"message"`
	RawOutput  string     `json:"raw_output"`
	Suggestion string     `json:"suggestion"`
}

var (
	// Deterministic signatures: cannot succeed by retrying
	reDeterministicSignatures = []*regexp.Regexp{
		regexp.MustCompile(`(?i)TS[0-9]{4}:`),
		regexp.MustCompile(`(?i)error TS[0-9]+`),
		regexp.MustCompile(`(?i)SyntaxError:`),
		regexp.MustCompile(`(?i)Cannot find module`),
		regexp.MustCompile(`(?i)ERR_MODULE_NOT_FOUND`),
		regexp.MustCompile(`(?i)Build error occurred`),
		regexp.MustCompile(`(?i)Failed to compile`),
		regexp.MustCompile(`(?i)could not compile`),
		regexp.MustCompile(`(?i)error\[E[0-9]{4}\]:`),
		regexp.MustCompile(`(?i)syntax error:`),
		regexp.MustCompile(`(?i)cannot find package`),
		regexp.MustCompile(`(?i)ModuleNotFoundError:`),
		regexp.MustCompile(`(?i)ImportError:`),
		regexp.MustCompile(`(?i)COPY failed:`),
		regexp.MustCompile(`(?i)file not found in build context`),
		regexp.MustCompile(`(?i)dockerfile parse error`),
		regexp.MustCompile(`(?i)unknown instruction:`),
		regexp.MustCompile(`(?i)bind: address already in use`),
		regexp.MustCompile(`(?i)failed to solve with frontend dockerfile\.v0`),
	}

	// Transient signatures: temporary infrastructure/network glitches
	reTransientSignatures = []*regexp.Regexp{
		regexp.MustCompile(`(?i)dial tcp.*i/o timeout`),
		regexp.MustCompile(`(?i)TLS handshake timeout`),
		regexp.MustCompile(`(?i)ETIMEDOUT`),
		regexp.MustCompile(`(?i)temporary failure in name resolution`),
		regexp.MustCompile(`(?i)network is unreachable`),
		regexp.MustCompile(`(?i)429 Too Many Requests`),
		regexp.MustCompile(`(?i)connection reset by peer`),
		regexp.MustCompile(`(?i)daemon is busy`),
	}

	// Application runtime signatures
	reAppCodeSignatures = []*regexp.Regexp{
		regexp.MustCompile(`(?i)Traceback \(most recent call last\)`),
		regexp.MustCompile(`(?i)panic:`),
		regexp.MustCompile(`(?i)UnhandledPromiseRejection`),
		regexp.MustCompile(`(?i)Uncaught Exception`),
		regexp.MustCompile(`(?i)NullPointerException`),
	}
)

// ClassifyError inspects output logs, stage, and exit code to classify the error.
func ClassifyError(stage string, exitCode int, stdout string, stderr string) ExecutionError {
	combined := stdout + "\n" + stderr

	// Check transient signatures first
	for _, re := range reTransientSignatures {
		if re.MatchString(combined) {
			return ExecutionError{
				Class:      ErrorClassTransient,
				Stage:      stage,
				ExitCode:   exitCode,
				Message:    "Transient infrastructure or network issue detected.",
				RawOutput:  combined,
				Suggestion: "Retry with exponential backoff (up to 2 retries).",
			}
		}
	}

	// Check application runtime signatures
	for _, re := range reAppCodeSignatures {
		if re.MatchString(combined) {
			return ExecutionError{
				Class:      ErrorClassApplicationCode,
				Stage:      stage,
				ExitCode:   exitCode,
				Message:    "Application source code runtime exception detected.",
				RawOutput:  combined,
				Suggestion: "Application source error. Review source code and runtime dependencies. ForgeOps will not modify source code.",
			}
		}
	}

	// Check deterministic compilation/syntax signatures
	for _, re := range reDeterministicSignatures {
		if re.MatchString(combined) {
			return ExecutionError{
				Class:      ErrorClassDeterministic,
				Stage:      stage,
				ExitCode:   exitCode,
				Message:    "Deterministic build, compilation, or manifest error detected.",
				RawOutput:  combined,
				Suggestion: "Fast-fail immediately. Do not retry without modifying configuration or manifests.",
			}
		}
	}

	// Default fallback: non-zero exit code during build is deterministic fast-fail
	if strings.Contains(strings.ToLower(stage), "build") || strings.Contains(strings.ToLower(stage), "compile") {
		return ExecutionError{
			Class:      ErrorClassDeterministic,
			Stage:      stage,
			ExitCode:   exitCode,
			Message:    "Build command terminated with non-zero exit code.",
			RawOutput:  combined,
			Suggestion: "Fast-fail on attempt 1. Review build logs.",
		}
	}

	return ExecutionError{
		Class:      ErrorClassEngineInternal,
		Stage:      stage,
		ExitCode:   exitCode,
		Message:    "Execution failed with unclassified error.",
		RawOutput:  combined,
		Suggestion: "Inspect engine logs and execution state.",
	}
}
