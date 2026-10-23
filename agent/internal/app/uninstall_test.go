// SPDX-License-Identifier: Apache-2.0
package app

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/config"
)

func TestUninstallCmd_HelpAndFlags(t *testing.T) {
	cfg := &config.Config{}
	app, err := New(cfg, BuildInfo{Version: "0.1.2"})
	if err != nil {
		t.Fatalf("New app: %v", err)
	}
	defer app.Close()

	cmd := newUninstallCmd(app)
	if cmd.Use != "uninstall" {
		t.Errorf("cmd.Use = %q, want uninstall", cmd.Use)
	}

	flags := []string{"dry-run", "force", "wipe-credentials", "json"}
	for _, f := range flags {
		if cmd.Flags().Lookup(f) == nil {
			t.Errorf("missing flag --%s", f)
		}
	}

	// Test dry-run execution
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	cmd.SetErr(&buf)
	cmd.SetArgs([]string{"--dry-run"})
	ctx := context.Background()

	err = cmd.ExecuteContext(ctx)
	if err != nil {
		t.Fatalf("cmd.ExecuteContext: %v", err)
	}

	output := buf.String()
	if !strings.Contains(output, "No ForgeOps agent") && !strings.Contains(output, "DRY RUN") {
		t.Errorf("unexpected output: %s", output)
	}
}
