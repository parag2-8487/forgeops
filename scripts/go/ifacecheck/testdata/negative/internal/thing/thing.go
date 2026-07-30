// SPDX-License-Identifier: Apache-2.0

// Package thing is a NEGATIVE fixture: Store is satisfied by MemStore, and there
// is deliberately no contract_test.go asserting it. ifacecheck must fail here.
//
// It lives under testdata/ so the Go tool ignores it, and in its own module so it
// never joins the agent's package graph.
package thing

// Store is an exported interface with one method, so it is auditable.
type Store interface {
	Get(key string) (string, bool)
}

// MemStore satisfies Store. No assertion exists anywhere in this module.
type MemStore struct {
	data map[string]string
}

// Get implements Store.
func (m *MemStore) Get(key string) (string, bool) {
	v, ok := m.data[key]
	return v, ok
}
