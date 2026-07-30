// SPDX-License-Identifier: Apache-2.0

// Package iac's interface obligations, stated in one greppable place
// (design.md §0.4.2). See internal/connection/contract_test.go for the rationale.
//
// `Runner` deliberately exposes no `apply` and Phase 1 does not add one (design
// §1.4, §14.6); this assertion is what makes a widened interface a compile error
// in one obvious place rather than a surprise at an injection site.
package iac

var _ Runner = (*TofuRunner)(nil)
