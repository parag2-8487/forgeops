// SPDX-License-Identifier: Apache-2.0

// Package identity's interface obligations, stated in one greppable place
// (design.md §0.4.2, §10.2).
//
// D-36 names two providers. `SpiffeWorkload` arrives with task 14.8, and its assertion
// belongs in this file when it does — `scripts/check-go-interface-assertions.sh` will
// require it the moment the type exists, so the obligation cannot be forgotten rather
// than merely being written down here.
package identity

var _ Provider = (*PairedDevice)(nil)
