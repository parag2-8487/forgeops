// SPDX-License-Identifier: Apache-2.0
package doctor

import (
	"testing"
)

func TestDiscoverDevTools(t *testing.T) {
	res := DiscoverDevTools()
	if len(res) == 0 {
		t.Fatalf("expected discovery map, got empty")
	}

	gitStatus, exists := res["git"]
	if !exists {
		t.Fatalf("expected git in devtools discovery results")
	}
	// git should exist in environment
	if !gitStatus.Installed {
		t.Logf("git not detected in PATH")
	}
}
