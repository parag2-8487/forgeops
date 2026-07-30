// SPDX-License-Identifier: Apache-2.0

// Package thing is a POSITIVE fixture: identical to the negative one except that
// contract_test.go carries the assertion. ifacecheck must pass here.
//
// Both fixtures are required. A checker that failed unconditionally would also
// pass its negative test, which would prove nothing.
package thing

// Store is an exported interface with one method, so it is auditable.
type Store interface {
	Get(key string) (string, bool)
}

// MemStore satisfies Store, and contract_test.go says so.
type MemStore struct {
	data map[string]string
}

// Get implements Store.
func (m *MemStore) Get(key string) (string, bool) {
	v, ok := m.data[key]
	return v, ok
}
