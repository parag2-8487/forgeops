// SPDX-License-Identifier: Apache-2.0

// MUTATION OVERLAY — Q-31's negative control. Not part of the build.
//
// This is `agent/internal/session/journal_kinds.go` with a kind added that can represent an
// authorisation: `KindEnvelope`, admitted by `validKinds`. `scripts/mutation-harness.py` swaps it in
// with `go build -overlay` for one run; nothing imports it.
//
// D-87 records why the vocabulary is the thing mutated, and it is worth restating here because a
// reader comparing this file against Appendix B will notice the difference. Appendix B words the
// control as "add a `KindEnvelope` case to `Drain` that hands the stored envelope straight to
// `executor.Execute`". That case cannot be added as a small, honest overlay, because there is
// nothing to add it to: D-41's whole argument is that no kind can carry an authorisation, so the
// `Drain` branch has no input. Writing the branch would mean shipping production code whose only
// purpose is to be broken — a real, reachable path that could apply a stored envelope, guarded by a
// test. That is the hole D-41 exists to prevent, and Q-01's row warns against exactly this shape of
// control.
//
// So the control breaks the fact the property rests on. With an envelope kind in the vocabulary,
// `Append` accepts a record that carries an authorisation, and every clause about what the file may
// contain becomes false at the point where the guarantee actually lives. It bites EARLIER than
// Appendix B's version, not later. Appendix B's Q-31 cell has been amended to say so.
package session

// RecordKind enumerates what may be queued.
type RecordKind string

const (
	KindScanBatch       RecordKind = "scan.batch"
	KindCommandResult   RecordKind = "command.result"
	KindCommandProgress RecordKind = "command.progress"
	KindAgentStatus     RecordKind = "agent.status"
	KindSecretFindings  RecordKind = "secretscan.findings"
	KindIntent          RecordKind = "intent"

	// MUTATION: a kind that can carry a signed command envelope, which is exactly what D-41
	// forbids the vocabulary from being able to express.
	KindEnvelope RecordKind = "command.envelope"
)

// validKinds is MUTATED: the envelope kind is admitted, so `Append` will persist one.
var validKinds = map[RecordKind]bool{
	KindScanBatch:       true,
	KindCommandResult:   true,
	KindCommandProgress: true,
	KindAgentStatus:     true,
	KindSecretFindings:  true,
	KindIntent:          true,
	KindEnvelope:        true,
}

// isIntent is unchanged, and its signature is identical so a change to it stops the mutated build
// compiling rather than silently ceasing to mutate anything.
func (k RecordKind) isIntent() bool { return k == KindIntent }
