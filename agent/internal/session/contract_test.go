// SPDX-License-Identifier: Apache-2.0

// Package session's interface obligations, stated in one greppable place
// (design.md §0.4.2, §10.3).
package session

import (
	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/identity"
)

var (
	_ Store    = (*FileStore)(nil)
	_ Journal  = (*FileJournal)(nil)
	_ Verifier = (*envelope.Verifier)(nil)

	// The mTLS dial reads its credential through this interface. It is declared in
	// `identity` — the CONSUMER — so neither package imports the other for it; see
	// internal/identity/paired_device.go for why the dependency points that way.
	// `session` does import `identity` (for the key pair and the CSR), which is the
	// direction §10.1 allows: session is layered above identity, never the reverse.
	_ identity.CredentialSource = (*FileStore)(nil)

	// The two envelope collaborators that have to read the credential Store. `envelope` is a
	// leaf and cannot reach the Store itself, so it declares the interfaces and this package
	// implements them (see bundle.go).
	_ envelope.KeySource = (*CredentialKeySource)(nil)

	// ONE type for both, and that is deliberate: `BundleState` gates mutations and
	// `BundleDigestSource` is Q-07's binding check, and both are asking which bundle this
	// agent holds. Two implementations could disagree, and the disagreement would look like
	// an intermittent policy failure rather than a wiring mistake.
	_ BundleState                 = (*CredentialBundleState)(nil)
	_ envelope.BundleDigestSource = (*CredentialBundleState)(nil)
)
