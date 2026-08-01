// SPDX-License-Identifier: Apache-2.0

// Package executor dispatches verified commands to named operations (design §7.7, §10.5).
//
// There is no `exec`, no `shell`, no `run_command`, and no operation whose argument is a
// command line. `phases.md` §1.1 requires named operations only, and the claim is checkable by
// reading this one file: the catalogue below is the whole vocabulary, and `dispatcher.go` is the
// only place any of it is reachable from.
package executor

import (
	"errors"
	"time"
)

// Operation is §7.7's closed catalogue.
//
// A string type rather than an integer enum so a wire value maps to a constant without a
// conversion table, and so a log line names the operation rather than an ordinal. Closedness is
// not enforceable by the Go type system for a string type, which is why it is enforced by the
// dispatch table instead: `Operations()` derives from the table, `ParseOperation` accepts only
// what the table holds, and a test asserts every constant declared here has an entry.
type Operation string

// The catalogue, in §7.7's order. Mutating operations are grouped last, deliberately: a reader
// checking "which of these can write" should find them together rather than interleaved.
const (
	OpProjectRegister   Operation = "project.register"
	OpProjectUnregister Operation = "project.unregister"

	OpScanFull        Operation = "scan.full"
	OpScanIncremental Operation = "scan.incremental"

	OpValidateCompose Operation = "validate.compose"
	OpValidateK8s     Operation = "validate.k8s"
	OpValidateTofu    Operation = "validate.tofu"
	OpValidateHelm    Operation = "validate.helm"
	OpValidateYAML    Operation = "validate.yaml"
	OpValidateTrivy   Operation = "validate.trivy"

	OpReadinessInventory Operation = "readiness.inventory"
	OpSecretScanRun      Operation = "secretscan.run"

	OpChangeSetApply      Operation = "changeset.apply"
	OpChangeSetRevert     Operation = "changeset.revert"
	OpGitBranchCommitPush Operation = "git.branch_commit_push"
	OpGitOpenPR           Operation = "git.open_pr"
	OpSecretsInject       Operation = "secrets.inject"
)

// allOperations is the declared vocabulary, used only to assert that the dispatch table covers
// it. It is NOT the dispatch surface: `handlerTable` is, and `Operations()` reads that table.
//
// Two lists of the same thing is the shape journal pattern H warns about, so the relationship
// between them is asserted in both directions — every constant has an entry, and every entry is
// a declared constant. Kept because the alternative is deriving the constants from the table,
// which would make a typo in a table key a silently new operation.
var allOperations = []Operation{
	OpProjectRegister, OpProjectUnregister,
	OpScanFull, OpScanIncremental,
	OpValidateCompose, OpValidateK8s, OpValidateTofu, OpValidateHelm, OpValidateYAML, OpValidateTrivy,
	OpReadinessInventory, OpSecretScanRun,
	OpChangeSetApply, OpChangeSetRevert, OpGitBranchCommitPush, OpGitOpenPR, OpSecretsInject,
}

// OperationInfo is what `agent.status` and `agent doctor` report about one operation (§10.5).
type OperationInfo struct {
	Operation Operation `json:"operation"`
	Mutating  bool      `json:"mutating"`
	// RequiresApproval mirrors §7.7's third column. Every mutating operation requires one, and
	// `TestEveryMutatingOperationRequiresAnApproval` asserts that rather than leaving it to
	// whoever adds the next row.
	RequiresApproval bool `json:"requires_approval"`
	// Timeout is the per-operation bound. Reported so an operator can see why something was
	// cut off, and so the numbers are reviewable in one place.
	Timeout time.Duration `json:"timeout"`
	// Implemented is false for a catalogued operation whose body arrives in a later group.
	// Reported rather than hidden: a backend that knows the agent will refuse `scan.full`
	// today can say so to a user instead of sending a command that comes back as an error.
	Implemented bool `json:"implemented"`
}

// Errors this package returns. Each maps to an `agent.error` code through Code().
var (
	// ErrUnknownOperation is A.2's step 6: an operation outside the closed catalogue. It lands
	// here rather than in `envelope` because the catalogue lives here and `envelope` is a leaf
	// package that cannot import it (D-59).
	ErrUnknownOperation = errors.New("executor: operation is not in the closed catalogue")

	// ErrApprovalRequired is D-83's half of the approval rule. `envelope.parse` no longer
	// requires a non-empty `approval_id`, because §7.7's read-only operations legitimately
	// carry it empty; the requirement is operation-dependent and therefore belongs where
	// operations are known. This is stronger than the blanket check it replaced: it
	// distinguishes the cases §7.7 distinguishes.
	ErrApprovalRequired = errors.New("executor: this operation mutates and requires an approval_id")

	// ErrUnimplemented is a catalogued operation with no body in this phase.
	//
	// Distinct from ErrUnknownOperation on purpose (D-85). Reporting "unknown" for an
	// operation that is in the catalogue would send the backend looking for a version skew
	// that does not exist, and would make the closed catalogue unreadable from the outside:
	// "we do not have that operation" and "we have it and it does nothing yet" are different
	// facts and only one of them is a bug.
	ErrUnimplemented = errors.New("executor: this operation is catalogued but not implemented in this phase")

	// ErrNoAuthority guards the whole surface: Execute takes a *envelope.Verified and refuses a
	// nil one rather than dereferencing it.
	ErrNoAuthority = errors.New("executor: a verified envelope is required")

	// ErrBadArgs is a malformed `args` object for an otherwise valid operation.
	ErrBadArgs = errors.New("executor: the operation's args are malformed")
)

// Code maps an error to its Appendix C.2 `agent.error` suffix.
//
// One function, so the vocabulary has one dialect (journal pattern R is a second, weaker
// dialect of an existing rule).
func Code(err error) string {
	switch {
	case err == nil:
		return ""
	case errors.Is(err, ErrUnknownOperation):
		return "operation-unknown"
	case errors.Is(err, ErrUnimplemented):
		return "operation-unimplemented"
	case errors.Is(err, ErrApprovalRequired):
		return "approval-required"
	case errors.Is(err, ErrNoAuthority):
		return "envelope-signature-invalid"
	case errors.Is(err, ErrBadArgs):
		return "envelope-malformed"
	default:
		return "operation-failed"
	}
}
