// SPDX-License-Identifier: Apache-2.0

// Package fileops' interface obligations, stated in one greppable place
// (design.md §0.4.2). See the note in internal/connection/contract_test.go for
// why absence rather than rot is the failure mode this guards.
package fileops

var _ Ops = (*FileOps)(nil)
