// SPDX-License-Identifier: Apache-2.0

// Package scanner's interface obligations, stated in one greppable place
// (design.md §0.4.2). See internal/connection/contract_test.go for the rationale.
package scanner

var _ Watcher = (*FSNotifyWatcher)(nil)

// `TokenFunc` is the function adapter the app layer uses to read the device token at CALL time
// rather than capturing it at construction — a rotated token would otherwise keep being sent after
// it stopped being valid. Asserted here because `check-go-interface-assertions` will not accept an
// implementation whose only proof is that the code happens to compile at one call site: a signature
// change would then break the caller rather than the contract.
var _ TokenSource = (TokenFunc)(nil)
