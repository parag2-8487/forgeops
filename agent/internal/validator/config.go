// SPDX-License-Identifier: Apache-2.0
package validator

import (
	_ "embed"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// The yamllint configuration travels inside the binary.
//
// EMBEDDED RATHER THAN READ FROM THE REPOSITORY. The agent is a host binary installed on a user's
// machine; `config/yamllint-artifacts.yaml` is in this repository and will not be there. Reading it
// from disk would make the validator's rules depend on the agent's working directory, so the same
// artifact would be judged differently depending on where the agent was started — and on a machine
// without the file at all, yamllint would silently fall back to its default config, whose `truthy`
// rule rejects every valid GitHub Actions workflow.
//
// The embedded copy is written to a per-process temp file on first use. One file for the process
// lifetime rather than one per validation: yamllint takes a path, and writing it per call would make
// a directory of workflows N file creations instead of one.
//
//go:embed yamllint-artifacts.yaml
var yamllintConfig []byte

var (
	yamllintConfigOnce sync.Once
	yamllintConfigPath string
	yamllintConfigErr  error
)

// yamllintConfigFile materialises the embedded config and returns its path.
func yamllintConfigFile() (string, error) {
	yamllintConfigOnce.Do(func() {
		dir, err := os.MkdirTemp("", "forgeops-yamllint-")
		if err != nil {
			yamllintConfigErr = fmt.Errorf("validator: cannot stage the yamllint config: %w", err)
			return
		}
		path := filepath.Join(dir, "yamllint.yaml")
		if err := os.WriteFile(path, yamllintConfig, 0o600); err != nil {
			yamllintConfigErr = fmt.Errorf("validator: cannot write the yamllint config: %w", err)
			return
		}
		yamllintConfigPath = path
	})
	return yamllintConfigPath, yamllintConfigErr
}
