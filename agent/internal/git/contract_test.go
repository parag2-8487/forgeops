// SPDX-License-Identifier: Apache-2.0

// Package git's interface obligations, stated in one greppable place
// (design.md §0.4.2). See internal/connection/contract_test.go for the rationale.
//
// `TokenSource` is the D-5 / D-38 seam: Phase 1 adds `AppInstallationTokenSource`
// beside `EnvTokenSource`, and this file is where that arrival becomes visible.
package git

var _ TokenSource = (*EnvTokenSource)(nil)
