// SPDX-License-Identifier: Apache-2.0
package git

import (
	"context"
	"os"
	"time"
)

// TokenSource is the auth seam. Phase 0 ships EnvTokenSource only.
// Phase 1 adds AppInstallationTokenSource behind this same interface.
type TokenSource interface {
	Token(ctx context.Context) (string, error)
}

// EnvTokenSource reads a personal access token from an environment variable.
type EnvTokenSource struct {
	EnvVar string // e.g. "GITHUB_TOKEN"
}

// Token returns the token value from the configured environment variable.
// Returns ErrTokenMissing if the variable is unset or empty.
func (e *EnvTokenSource) Token(_ context.Context) (string, error) {
	v := os.Getenv(e.EnvVar)
	if v == "" {
		return "", &ErrTokenMissing{EnvVar: e.EnvVar}
	}
	return v, nil
}

// Signature identifies a commit author/committer.
type Signature struct {
	Name  string
	Email string
}

// ChangeSet represents a set of file changes to commit.
type ChangeSet struct {
	BaseBranch string
	Branch     string   // e.g. "forgeops/chore-scaffold-20260726T120000Z"
	Paths      []string // repo-relative, must resolve inside the repo root
	Message    string
	Author     Signature
}

// Commit is the result of a successful commit.
type Commit struct {
	SHA     string
	Message string
}

// PullRequestRequest describes a PR to create.
type PullRequestRequest struct {
	Owner string
	Repo  string
	Title string
	Body  string
	Head  string // branch name
	Base  string // target branch
}

// PullRequest is the result of creating a PR.
type PullRequest struct {
	Number int
	URL    string
	State  string
}

// PRStatus describes the current state of a pull request.
type PRStatus struct {
	Number         int
	State          string // "open" | "closed" | "merged"
	ReviewDecision string // "approved" | "changes_requested" | "review_required" | "pending"
	Mergeable      *bool
	HeadSHA        string
	UpdatedAt      time.Time
}

// Client is the Git/PR operations interface.
// All types exposed through this interface are project-owned — no library types leak.
type Client interface {
	// CreateBranch creates a new branch from the specified base.
	CreateBranch(ctx context.Context, repo string, base, branch string) error

	// CommitPaths stages and commits the specified paths.
	CommitPaths(ctx context.Context, repo string, cs ChangeSet) (Commit, error)

	// Push pushes the branch to the remote. Force-push is never supported.
	Push(ctx context.Context, repo, branch string) error

	// OpenPullRequest creates a new pull request.
	OpenPullRequest(ctx context.Context, req PullRequestRequest) (PullRequest, error)

	// PullRequestStatus retrieves the current status of a pull request.
	PullRequestStatus(ctx context.Context, owner, name string, number int) (PRStatus, error)

	// PollUntil polls the PR status at interval until a terminal state or timeout.
	// Terminal states: closed, merged, approved, changes_requested.
	// Returns last observed status on timeout (no error).
	PollUntil(ctx context.Context, owner, name string, number int, interval, timeout time.Duration) (PRStatus, error)
}
