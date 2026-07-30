// SPDX-License-Identifier: Apache-2.0
//
// Standalone module for the interface-assertion checker (design.md 0.4.2).
//
// It has an EMPTY require list on purpose: everything it needs is in the standard
// library, so no tool dependency enters agent/go.mod, the shipped module graph
// that D-1's cgo guard and the release SBOM both police. No go.sum exists or is
// needed, which also means this check cannot be a supply-chain surface.
module github.com/parag8487/ForgeOps/scripts/go/ifacecheck

go 1.26
