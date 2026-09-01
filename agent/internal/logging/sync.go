// SPDX-License-Identifier: Apache-2.0

package logging

import (
	"errors"
	"syscall"

	"go.uber.org/zap"
)

// Sync flushes the logger, treating "this stream cannot be synced" as success.
//
// WHY THIS EXISTS. Every invocation of the agent on Windows ended with:
//
//	shutdown: logger: sync /dev/stderr: The handle is invalid.
//
// on SUCCESSFUL runs as well as failures. `fsync` is meaningless on a console handle, so Windows
// answers `ERROR_INVALID_HANDLE`; zap surfaces that from `Sync`, the shutdown sequence reported it
// as a closer failure, and it printed ABOVE the real message. It reads as a crash, and it trains a
// user to skim the last lines of output — which is the actual harm, because the line underneath is
// the one that matters. The same thing happens on Linux and macOS whenever stderr is a pipe or a
// terminal, where the answer is `EINVAL` or `ENOTSUP` instead.
//
// WHAT IT DOES NOT DO. It does not ignore sync failures generally, and it does not ignore them on
// Windows generally. Only the specific errno values that mean "this file descriptor does not
// support flushing" are treated as success. `ENOSPC`, `EIO` and `EBADF` are real: a full disk or a
// closed descriptor while writing a log is a fact the operator needs, and a log sink that is a
// FILE reports those through this same path. Suppressing the whole error on Windows — the obvious
// one-line fix — would have thrown those away with it.
func Sync(logger *zap.Logger) error {
	if logger == nil {
		return nil
	}
	err := logger.Sync()
	if err == nil || isStreamThatCannotSync(err) {
		return nil
	}
	return err
}

// isStreamThatCannotSync reports whether the error is a refusal to flush a stream that never
// needed flushing, rather than a failure to flush one that did.
//
// Matched on the errno rather than on the message. Comparing against "The handle is invalid."
// would break under any non-English Windows locale, and matching a substring of an OS error string
// is how a check like this silently stops working.
func isStreamThatCannotSync(err error) bool {
	var errno syscall.Errno
	if !errors.As(err, &errno) {
		return false
	}
	for _, candidate := range unsyncableErrnos() {
		if errno == candidate {
			return true
		}
	}
	return false
}

// posixUnsyncableErrnos are the answers a POSIX kernel gives when the target of `fsync` is a
// terminal or a pipe.
//
//   - EINVAL: Linux returns this for a pipe or a socket.
//   - ENOTSUP: some filesystems and macOS devices report it this way.
//   - ENOTTY: returned by older BSD-derived systems for a character device.
//
// EBADF is deliberately absent. A bad descriptor means the log sink was closed underneath the
// logger, which is a real defect and must not be swallowed.
var posixUnsyncableErrnos = []syscall.Errno{
	syscall.EINVAL,
	syscall.ENOTSUP,
	syscall.ENOTTY,
}
