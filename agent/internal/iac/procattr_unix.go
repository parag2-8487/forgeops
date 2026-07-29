// SPDX-License-Identifier: Apache-2.0
//go:build !windows

package iac

import (
	"os/exec"
	"syscall"
	"time"
)

// setProcessGroup creates a new process group for the child so we can
// signal tofu AND every provider plugin it spawned.
func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// terminateGroup sends SIGTERM to the entire process group, waits for
// the grace period, then sends SIGKILL if still alive.
//
// It deliberately does NOT call cmd.Wait: Wait is owned by the caller, and a
// second call returns "Wait was already called" immediately. An earlier revision
// waited on that call, so the done channel closed at once, the grace period never
// elapsed and the SIGKILL escalation below was unreachable — a provider plugin
// that ignored SIGTERM survived as an orphan. Polling with signal 0 asks the
// kernel whether the group still exists without consuming the exit status.
func terminateGroup(cmd *exec.Cmd, grace time.Duration) {
	if cmd.Process == nil {
		return
	}
	pgid := -cmd.Process.Pid // negative pid targets the whole group
	_ = syscall.Kill(pgid, syscall.SIGTERM)

	deadline := time.Now().Add(grace)
	for time.Now().Before(deadline) {
		if err := syscall.Kill(pgid, 0); err != nil {
			return // the whole group is gone
		}
		time.Sleep(20 * time.Millisecond)
	}
	_ = syscall.Kill(pgid, syscall.SIGKILL)
}
