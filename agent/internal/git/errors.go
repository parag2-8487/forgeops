// SPDX-License-Identifier: Apache-2.0
package git

import (
	"fmt"
	"time"
)

// ErrTokenMissing is returned when the expected environment variable is unset or empty.
// The error message includes the variable name but never the token value.
type ErrTokenMissing struct {
	EnvVar string
}

func (e *ErrTokenMissing) Error() string {
	return fmt.Sprintf("token not set: environment variable %s is empty", e.EnvVar)
}

// ErrPushRejected is returned when a git push is rejected by the remote.
type ErrPushRejected struct {
	Branch string
	Reason string
}

func (e *ErrPushRejected) Error() string {
	return fmt.Sprintf("push rejected for branch %s: %s", e.Branch, e.Reason)
}

// ErrGitAuth is returned when git authentication fails.
type ErrGitAuth struct {
	Reason string
}

func (e *ErrGitAuth) Error() string {
	return fmt.Sprintf("git authentication failed: %s", e.Reason)
}

// ErrRateLimited is returned when the GitHub API rate limit is exceeded.
type ErrRateLimited struct {
	ResetAt time.Time
}

func (e *ErrRateLimited) Error() string {
	return fmt.Sprintf("rate limited: resets at %s", e.ResetAt.Format(time.RFC3339))
}

// ErrPathOutsideRepo is returned when a path resolves outside the repository root.
type ErrPathOutsideRepo struct {
	Path string
}

func (e *ErrPathOutsideRepo) Error() string {
	return fmt.Sprintf("path resolves outside repository root: %s", e.Path)
}
