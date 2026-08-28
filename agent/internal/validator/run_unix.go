// SPDX-License-Identifier: Apache-2.0
//go:build !windows

package validator

import (
	"os/exec"
	"syscall"
)

// configureProcessGroup puts the child in its own process group.
//
// Needed because the tools here spawn children of their own: `tofu` runs provider plugins, `helm`
// can run a post-renderer, `trivy` may fork for its database. Signalling only the parent leaves
// those behind, and an operation whose timeout fires would return while work continued in the
// background — which on a validate-then-generate loop means the next iteration races the previous
// one's leftovers.
func configureProcessGroup(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}

// terminateGroup signals the whole group, politely first.
//
// SIGTERM rather than SIGKILL, with `Cmd.WaitDelay` in run.go escalating if the group ignores it.
// A tool killed outright can leave a partially written plan file or a locked provider cache, and
// the next run then fails for a reason that has nothing to do with the artifact.
func terminateGroup(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
}
