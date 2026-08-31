// SPDX-License-Identifier: Apache-2.0
package executor

import "sync"

// secretEnvironment holds injected secret values in process memory, per project (FR-45).
//
// MEMORY AND NOT DISK, AND THAT IS THE REQUIREMENT RATHER THAN A PREFERENCE. FR-45 says secrets reach a
// deployment as environment variables and are "never written into a generated file". A `.env` would
// satisfy the first clause and break the second, and `fileops`' write blocklist refuses `.env` anyway —
// deliberately, because a file is readable by every process the user runs, survives a reboot, and gets
// committed by accident.
//
// The consequence is that injected values do not survive an agent restart, and that is correct: a
// credential that outlives the session it was approved for is a credential nobody revoked. An operator
// who restarts the agent injects again, which is one command, and the alternative is a secret store the
// agent quietly maintains.
//
// NO ACCESSOR RETURNS A VALUE TO A HANDLER. `env` returns the mapping for a deploy command to pass to a
// child process, and nothing in this package reads it into a `Result`. That is what keeps a value out of
// `command.result`, which travels over the websocket, is persisted, and reaches an append-only
// hash-chained audit trail — where it would become the most durable copy of the secret.
type secretEnvironment struct {
	mu sync.Mutex
	//: project id -> variable name -> value.
	byProject map[string]map[string]string
}

func newSecretEnvironment() *secretEnvironment {
	return &secretEnvironment{byProject: map[string]map[string]string{}}
}

// put stores values for a project and returns the names that already had one.
//
// The replaced names are reported because an operator re-running an injection with one credential
// rotated should be able to see that it landed, and a silent overwrite of a production value is exactly
// the event worth naming.
func (s *secretEnvironment) put(projectID string, values map[string]string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	existing, ok := s.byProject[projectID]
	if !ok {
		existing = map[string]string{}
		s.byProject[projectID] = existing
	}
	replaced := make([]string, 0, len(values))
	for key, value := range values {
		if _, present := existing[key]; present {
			replaced = append(replaced, key)
		}
		existing[key] = value
	}
	return replaced
}

// env returns a copy of the injected environment for a project.
//
// A COPY, so a caller cannot mutate the store by holding the map, and so a deploy command building its
// own environment cannot leak one project's credentials into another's by aliasing.
func (s *secretEnvironment) env(projectID string) map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	stored, ok := s.byProject[projectID]
	if !ok {
		return nil
	}
	copied := make(map[string]string, len(stored))
	for key, value := range stored {
		copied[key] = value
	}
	return copied
}

// keys returns the variable names injected for a project, without their values.
//
// The only accessor anything reporting to a user should need.
func (s *secretEnvironment) keys(projectID string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	stored := s.byProject[projectID]
	names := make([]string, 0, len(stored))
	for key := range stored {
		names = append(names, key)
	}
	return names
}

// forget drops a project's injected values.
func (s *secretEnvironment) forget(projectID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.byProject, projectID)
}
