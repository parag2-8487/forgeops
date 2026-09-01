// SPDX-License-Identifier: Apache-2.0

//go:build !windows

package logging

import "syscall"

// unsyncableErrnos is the POSIX set. Linux answers `EINVAL` when stderr is a pipe, which is how
// the agent runs under `docker compose exec -d` and in every CI job, so this is not a Windows-only
// concern — it was simply never reported as a closer failure there because the journey redirects
// stderr to a file, which syncs cleanly.
func unsyncableErrnos() []syscall.Errno {
	return posixUnsyncableErrnos
}
