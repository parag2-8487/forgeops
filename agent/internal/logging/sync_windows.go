// SPDX-License-Identifier: Apache-2.0

//go:build windows

package logging

import (
	"syscall"

	"golang.org/x/sys/windows"
)

// unsyncableErrnos adds the Windows answer to `fsync` on a console handle.
//
// `ERROR_INVALID_HANDLE` is what produced "sync /dev/stderr: The handle is invalid." on every
// single agent invocation, successful ones included. Taken from `x/sys/windows` — already a direct
// dependency — rather than written as the literal 6, so it is checkable against the Windows error
// list instead of being a magic number. `syscall` does not export it.
//
// The POSIX set is included as well and that is not redundant: Go maps several Windows errors onto
// the POSIX names, so a redirected stderr under a Windows shell can surface as `EINVAL`.
func unsyncableErrnos() []syscall.Errno {
	return append([]syscall.Errno{syscall.Errno(windows.ERROR_INVALID_HANDLE)}, posixUnsyncableErrnos...)
}
