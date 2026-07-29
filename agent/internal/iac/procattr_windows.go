// SPDX-License-Identifier: Apache-2.0
//go:build windows

package iac

import (
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

// Windows Job Objects would be stricter but are recorded as Phase 1 hardening (OQ-6).
const _CREATE_NEW_PROCESS_GROUP = 0x00000200

func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: _CREATE_NEW_PROCESS_GROUP,
	}
}

func terminateGroup(cmd *exec.Cmd, grace time.Duration) {
	if cmd.Process == nil {
		return
	}
	// taskkill /T kills the process tree. OQ-6 records Job Objects as Phase 1 hardening.
	_ = exec.Command("taskkill", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F").Run()
}
