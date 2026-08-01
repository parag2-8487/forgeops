// SPDX-License-Identifier: Apache-2.0

// Package session's interface obligations, stated in one greppable place
// (design.md §0.4.2, §10.3).
package session

import "github.com/parag8487/ForgeOps/agent/internal/identity"

var (
	_ Store   = (*FileStore)(nil)
	_ Journal = (*FileJournal)(nil)

	// The mTLS dial reads its credential through this interface. It is declared in
	// `identity` — the CONSUMER — so neither package imports the other for it; see
	// internal/identity/paired_device.go for why the dependency points that way.
	// `session` does import `identity` (for the key pair and the CSR), which is the
	// direction §10.1 allows: session is layered above identity, never the reverse.
	_ identity.CredentialSource = (*FileStore)(nil)
)
