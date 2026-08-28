// SPDX-License-Identifier: Apache-2.0
//go:build windows

package validator

import (
	"os/exec"
	"syscall"
)

// configureProcessGroup puts the child at the head of a new process group.
//
// Windows has no process groups in the POSIX sense; `CREATE_NEW_PROCESS_GROUP` is the nearest
// equivalent and is what makes it possible to signal the child's descendants rather than only the
// child. The agent is a host binary and Windows is a first-class target for it — the release builds
// windows/amd64 and windows/arm64 — so this is a supported path rather than a courtesy.
func configureProcessGroup(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.CreationFlags |= syscall.CREATE_NEW_PROCESS_GROUP
}

// terminateGroup ends the child.
//
// `Process.Kill` rather than a console control event: `GenerateConsoleCtrlEvent` only reaches
// processes attached to the same console, and the agent may be running as a service with none. The
// grace window in `Cmd.WaitDelay` still applies, so a tool that finishes on its own during the
// window is not killed at all.
func terminateGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return cmd.Process.Kill()
}
