// SPDX-License-Identifier: Apache-2.0
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/parag8487/ForgeOps/agent/internal/app"
	"github.com/parag8487/ForgeOps/agent/internal/config"
)

var (
	version = "dev"
	commit  = "none"
	date    = "unknown"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "fatal: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return fmt.Errorf("load config: %w", err)
	}

	a, err := app.New(cfg, app.BuildInfo{Version: version, Commit: commit, Date: date})
	if err != nil {
		return fmt.Errorf("build app: %w", err)
	}
	defer func() {
		if cerr := a.Close(); cerr != nil {
			fmt.Fprintf(os.Stderr, "shutdown: %v\n", cerr)
		}
	}()

	if err := app.NewRootCommand(a).ExecuteContext(ctx); err != nil && !errors.Is(err, context.Canceled) {
		return err
	}
	return nil
}
