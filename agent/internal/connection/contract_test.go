// SPDX-License-Identifier: Apache-2.0

// Package connection's interface obligations, stated in one greppable place
// (design.md §0.4.2).
//
// A compile-time assertion cannot rot: the compiler rechecks it on every build.
// It can only be absent, and an absent assertion is indistinguishable from a
// satisfied one until a signature changes and only the injection site breaks — at
// runtime. That is the Go shape of the Phase 0 D-23 defect.
//
// `scripts/check-go-interface-assertions.sh` fails the build when a type that
// structurally satisfies an exported interface has no assertion here.
package connection

var _ Transport = (*WSSTransport)(nil)
