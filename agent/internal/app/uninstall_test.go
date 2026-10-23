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

func TestRootCommand_RegistersUninstallCommand(t *testing.T) {
	cfg := &config.Config{}
	app, err := New(cfg, BuildInfo{Version: "0.1.2", Commit: "test-commit", Date: "2026-10-24"})
	if err != nil {
		t.Fatalf("New app: %v", err)
	}
	defer app.Close()

	root := NewRootCommand(app)

	// 1. Assert command discovery via root.Find
	subCmd, _, findErr := root.Find([]string{"uninstall"})
	if findErr != nil || subCmd == nil {
		t.Fatalf("root.Find('uninstall') failed: %v", findErr)
	}
	if subCmd.Name() != "uninstall" {
		t.Errorf("subCmd.Name() = %q, want uninstall", subCmd.Name())
	}

	// 2. Assert root --help lists the uninstall command
	var helpBuf bytes.Buffer
	root.SetOut(&helpBuf)
	root.SetErr(&helpBuf)
	root.SetArgs([]string{"--help"})
	if err := root.ExecuteContext(context.Background()); err != nil {
		t.Fatalf("root --help execution failed: %v", err)
	}
	helpOut := helpBuf.String()
	if !strings.Contains(helpOut, "uninstall") {
		t.Errorf("root --help output missing 'uninstall' command. Output:\n%s", helpOut)
	}
	if !strings.Contains(helpOut, "Uninstall all verified ForgeOps agent installations") {
		t.Errorf("root --help output missing uninstall description. Output:\n%s", helpOut)
	}

	// 3. Assert root 'uninstall --help' works and describes supported flags
	var uninstallHelpBuf bytes.Buffer
	root.SetOut(&uninstallHelpBuf)
	root.SetErr(&uninstallHelpBuf)
	root.SetArgs([]string{"uninstall", "--help"})
	if err := root.ExecuteContext(context.Background()); err != nil {
		t.Fatalf("root uninstall --help failed: %v", err)
	}
	unHelpOut := uninstallHelpBuf.String()
	for _, flag := range []string{"--dry-run", "--force", "--wipe-credentials", "--json"} {
		if !strings.Contains(unHelpOut, flag) {
			t.Errorf("uninstall --help output missing %s. Output:\n%s", flag, unHelpOut)
		}
	}

	// 4. Assert root 'uninstall --dry-run' executes cleanly
	var dryBuf bytes.Buffer
	root.SetOut(&dryBuf)
	root.SetErr(&dryBuf)
	root.SetArgs([]string{"uninstall", "--dry-run"})
	if err := root.ExecuteContext(context.Background()); err != nil {
		t.Fatalf("root uninstall --dry-run failed: %v", err)
	}

	// 5. Assert root 'uninstall --force' executes cleanly without hanging or asking interactive prompts
	var forceBuf bytes.Buffer
	root.SetOut(&forceBuf)
	root.SetErr(&forceBuf)
	root.SetArgs([]string{"uninstall", "--force"})
	if err := root.ExecuteContext(context.Background()); err != nil {
		t.Fatalf("root uninstall --force failed: %v", err)
	}
	forceOut := forceBuf.String()
	if !strings.Contains(forceOut, "ForgeOps agent") && !strings.Contains(forceOut, "Uninstall summary") {
		t.Errorf("unexpected output from uninstall --force:\n%s", forceOut)
	}
}
