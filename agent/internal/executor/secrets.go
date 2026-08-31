// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

// `secrets.inject` — deploy-time secret injection (FR-45).
//
// WHAT "AT DEPLOY TIME" MEANS HERE, AND WHY NOTHING IS WRITTEN TO DISK
// -------------------------------------------------------------------
// FR-45 requires secrets to reach a deployment as environment variables, never written into a
// generated file and never included in an LLM prompt. Those three clauses are what this operation is
// shaped by, and the third one has nothing to do with the agent — a prompt is assembled in the backend
// and `secrets/redaction.py` covers it.
//
// The first two are here, and they rule out the obvious implementation. Writing a `.env` would satisfy
// "environment variables" and violate "never written into a generated file", and `fileops`' blocklist
// refuses `.env` for writing anyway, deliberately. So injected values live in the agent's process
// memory, keyed per project, and are handed to a deploy command's environment when one runs. Nothing
// touches the filesystem.
//
// MUTATING AND APPROVAL-REQUIRED, and it does not write a byte. That is not a contradiction: the
// operation changes what a subsequent deployment will do, which is the thing an approver is being asked
// about. Classifying it as a read because it happens not to call `os.WriteFile` would let a change to a
// production environment through without a human, which is the opposite of §7.7's intent.
//
// THE RESULT NEVER CARRIES A VALUE. It reports the KEYS injected and their count. A `command.result`
// travels back over the websocket, is persisted, and reaches an append-only hash-chained audit trail —
// so a value in it would make the tamper-evident log the most durable copy of the secret.

// secretsInjectArgs is the argument object for `secrets.inject`.
//
// The values arrive inside the signed envelope, which is what makes this safe to send at all: the
// envelope is sealed to the device's own key (D-62) and verified before a handler ever sees it, so a
// value is not readable in transit and cannot be replayed to a different device.
type secretsInjectArgs struct {
	ProjectID string `json:"project_id"`
	// Values maps environment variable name to value.
	Values map[string]string `json:"values"`
}

// SecretInjectionReport is what `secrets.inject` reports. Keys only, never values.
type SecretInjectionReport struct {
	ProjectID string `json:"project_id"`
	// Keys are the variable names injected, sorted, so two identical injections produce an identical
	// report and a difference between them means something changed.
	Keys []string `json:"keys"`
	// Count is separate from `len(Keys)` for the same reason a scan reports both: if a bound is ever
	// applied to the list, the count stays the truth.
	Count int `json:"count"`
	// Replaced names the keys that already had a value for this project and were overwritten. An
	// operator re-running an injection with one key changed should be able to see that, and a silent
	// overwrite of a production credential is exactly the event worth reporting.
	Replaced []string `json:"replaced,omitempty"`
}

// : A bound on how many variables one injection may carry. Not a security control — the envelope is
// : already signed and size-bounded — but a deployment needing more than this is a configuration
// : mistake worth refusing rather than absorbing.
const maxInjectedSecrets = 256

// : Refused variable names. `PATH` and `LD_PRELOAD` decide which BINARY a later command runs, so an
// : injected value for either turns a secret injection into arbitrary code execution on the user's
// : machine, under an approval a human granted for something else entirely.
var refusedSecretKeys = map[string]struct{}{
	"PATH":                  {},
	"LD_PRELOAD":            {},
	"LD_LIBRARY_PATH":       {},
	"DYLD_INSERT_LIBRARIES": {},
	"NODE_OPTIONS":          {},
	"PYTHONPATH":            {},
	"PYTHONSTARTUP":         {},
}

func decodeSecretsInjectArgs(v *envelope.Verified) (secretsInjectArgs, error) {
	var args secretsInjectArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return secretsInjectArgs{}, fmt.Errorf("executor: undecodable secrets.inject arguments: %w", err)
	}
	if strings.TrimSpace(args.ProjectID) == "" {
		return secretsInjectArgs{}, errors.New("executor: secrets.inject needs a project_id")
	}
	if len(args.Values) == 0 {
		// An injection of nothing is a caller mistake, not a no-op to absorb: it would report success
		// and leave a deployment without the credentials it was told it would have.
		return secretsInjectArgs{}, errors.New("executor: secrets.inject was given no values")
	}
	if len(args.Values) > maxInjectedSecrets {
		return secretsInjectArgs{}, fmt.Errorf(
			"executor: secrets.inject was given %d values, more than the %d bound",
			len(args.Values), maxInjectedSecrets)
	}
	for key := range args.Values {
		trimmed := strings.TrimSpace(key)
		if trimmed == "" {
			return secretsInjectArgs{}, errors.New("executor: secrets.inject was given an empty variable name")
		}
		if trimmed != key {
			// A name with surrounding whitespace is almost always a parsing accident, and it would
			// produce a variable no process can read.
			return secretsInjectArgs{}, fmt.Errorf("executor: variable name %q has surrounding whitespace", key)
		}
		if strings.ContainsAny(key, "=\x00") {
			return secretsInjectArgs{}, fmt.Errorf("executor: variable name %q contains = or NUL", key)
		}
		if _, refused := refusedSecretKeys[strings.ToUpper(key)]; refused {
			return secretsInjectArgs{}, fmt.Errorf(
				"executor: %s decides which binary a later command runs and will not be injected; "+
					"injecting it would turn an approved secret injection into arbitrary code execution", key)
		}
	}
	return args, nil
}

// secretsInject holds the values in memory for a later deployment, and reports only the key names.
func secretsInject(_ context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	args, err := decodeSecretsInjectArgs(v)
	if err != nil {
		return Result{}, err
	}
	sink.Progress(20, "secrets.inject", fmt.Sprintf("injecting %d variable(s)", len(args.Values)))

	replaced := d.secrets.put(args.ProjectID, args.Values)

	keys := make([]string, 0, len(args.Values))
	for key := range args.Values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	sort.Strings(replaced)

	report := SecretInjectionReport{
		ProjectID: args.ProjectID,
		Keys:      keys,
		Count:     len(keys),
		Replaced:  replaced,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable injection report: %w", err)
	}
	// The message names the keys, deliberately: an operator watching a deployment needs to know WHICH
	// credentials were supplied, and the names are not the secret.
	sink.Progress(100, "secrets.inject", "injected "+strings.Join(keys, ", "))
	return Result{Status: "injected", Output: string(encoded)}, nil
}
