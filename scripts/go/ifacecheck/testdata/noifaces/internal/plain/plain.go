// SPDX-License-Identifier: Apache-2.0

// Package plain has a concrete type and NO interface, so ifacecheck must fail on
// an empty interface set rather than report success.
package plain

// Widget is concrete and implements nothing.
type Widget struct{ Name string }

// Describe is a method, so Widget has a non-empty method set.
func (w Widget) Describe() string { return w.Name }
