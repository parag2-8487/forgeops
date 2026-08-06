// SPDX-License-Identifier: Apache-2.0
package doctor

import (
	"os/exec"
)

type ToolStatus struct {
	Name      string
	Installed bool
	Path      string
}

func DiscoverDevTools() map[string]ToolStatus {
	tools := []string{"docker", "kubectl", "helm", "tofu", "trivy", "git"}
	results := make(map[string]ToolStatus)

	for _, t := range tools {
		path, err := exec.LookPath(t)
		results[t] = ToolStatus{
			Name:      t,
			Installed: err == nil,
			Path:      path,
		}
	}

	return results
}
