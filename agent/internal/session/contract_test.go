// SPDX-License-Identifier: Apache-2.0

// Package session's interface obligations, stated in one greppable place
// (design.md §0.4.2, §10.3).
package session

var (
	_ Store   = (*FileStore)(nil)
	_ Journal = (*FileJournal)(nil)
)

// FileStore also satisfies identity.CredentialSource once task 8.3 wires pairing, and
// that assertion belongs here when the method lands. The interface is declared in
// `identity` — the consumer — so neither package imports the other; see
// internal/identity/paired_device.go for why the dependency points that way.
