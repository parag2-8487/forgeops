// SPDX-License-Identifier: Apache-2.0

// The journal's record vocabulary, in its own file.
//
// Separated for two reasons, and the second is the one that matters.
//
// The mechanical one: `go build -overlay` replaces a whole file, and Q-31's negative control adds a
// kind capable of representing an authorisation. With this table inside `journal.go` the overlay
// would have carried a copy of four hundred and forty lines, rotting on the first unrelated edit.
// `rollback.go` (Q-01), `domain.go` (Q-14) and `order.go` (Q-15) were separated for the same reason.
//
// The substantive one: this table IS D-41's guarantee. The design's claim is not "we remember not to
// persist an authorisation" — it is that the type cannot express one. There is deliberately no kind
// for a command envelope, an approval response, an `approval_id`, a `MutationAuthority`, a device
// token, an envelope key or a secret value. A reader checking that claim should be able to check it
// by reading one short file, and a control that breaks it should be a one-line diff against that
// file (D-87).
package session

// RecordKind enumerates what may be queued. The list is closed, and what is absent from it is the
// point: nothing that AUTHORISES a mutation can be represented, so nothing that authorises a
// mutation can be persisted (D-41). That is why envelope expiry, seq allocation, revocation and
// policy staleness — D-41's items 1 to 4 — need no mitigation in the journal: they cannot arise.
type RecordKind string

const (
	KindScanBatch       RecordKind = "scan.batch"
	KindCommandResult   RecordKind = "command.result"
	KindCommandProgress RecordKind = "command.progress"
	KindAgentStatus     RecordKind = "agent.status"
	KindSecretFindings  RecordKind = "secretscan.findings" // metadata only, never values
	KindIntent          RecordKind = "intent"              // replayed as approval.request
)

// validKinds is the closed set. A record with any other kind is refused at Append, so an
// unknown kind cannot reach the file and be drained later by a version that understands
// it differently.
var validKinds = map[RecordKind]bool{
	KindScanBatch:       true,
	KindCommandResult:   true,
	KindCommandProgress: true,
	KindAgentStatus:     true,
	KindSecretFindings:  true,
	KindIntent:          true,
}

// isIntent selects the records drained SECOND, after everything else has been acknowledged.
//
// Ordering matters: a scan batch delivered after an intent would let the backend evaluate the
// intent against an index it is about to replace.
func (k RecordKind) isIntent() bool { return k == KindIntent }
