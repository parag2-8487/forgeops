// SPDX-License-Identifier: Apache-2.0

// Package telemetry's interface obligations, stated in one greppable place
// (design.md §0.4.2). See internal/connection/contract_test.go for the rationale.
package telemetry

var _ Tracer = (*NoopTracer)(nil)
