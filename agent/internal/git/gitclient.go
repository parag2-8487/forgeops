// SPDX-License-Identifier: Apache-2.0
package git

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	gogit "github.com/go-git/go-git/v5"
	gogitconfig "github.com/go-git/go-git/v5/config"
	"github.com/go-git/go-git/v5/plumbing"
	"github.com/go-git/go-git/v5/plumbing/object"
	gogithttp "github.com/go-git/go-git/v5/plumbing/transport/http"
	github "github.com/google/go-github/v68/github"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// gitClient implements the Client interface wrapping go-git and go-github.
type gitClient struct {
	cfg        Config
	tokens     TokenSource
	logger     *zap.Logger
	tracer     telemetry.Tracer
	httpClient *http.Client // optional, for testing
}

// NewClient constructs a Git/PR client. The libraries are wrapped, never leaked
// past this package.
func NewClient(cfg Config, tokens TokenSource, logger *zap.Logger, tracer telemetry.Tracer) Client {
	return &gitClient{
		cfg:    cfg,
		tokens: tokens,
		logger: logger,
		tracer: tracer,
	}
}

// NewClientWithHTTP constructs a Git/PR client with a custom HTTP client (for testing).
func NewClientWithHTTP(cfg Config, tokens TokenSource, logger *zap.Logger, tracer telemetry.Tracer, httpClient *http.Client) Client {
	return &gitClient{
		cfg:        cfg,
		tokens:     tokens,
		logger:     logger,
		tracer:     tracer,
		httpClient: httpClient,
	}
}

// CreateBranch opens a repository at the given path and creates a new branch
// from the specified base ref.
func (g *gitClient) CreateBranch(ctx context.Context, repo string, base, branch string) error {
	// go-git's local API takes no context, so the span is opened for timing and
	// nesting only; there is no ctx-consuming call below to thread it into.
	_, span := g.tracer.StartSpan(ctx, "git.CreateBranch")
	defer span.End()

	r, err := gogit.PlainOpen(repo)
	if err != nil {
		return fmt.Errorf("open repo: %w", err)
	}

	// Resolve the base ref. Try as a branch first, then as a hash.
	var baseHash plumbing.Hash
	baseRef, err := r.Reference(plumbing.NewBranchReferenceName(base), true)
	if err != nil {
		// Try resolving as a remote tracking branch.
		baseRef, err = r.Reference(plumbing.NewRemoteReferenceName("origin", base), true)
		if err != nil {
			// Try resolving HEAD if base is empty or "HEAD".
			if base == "" || base == "HEAD" {
				head, herr := r.Head()
				if herr != nil {
					return fmt.Errorf("resolve HEAD: %w", herr)
				}
				baseHash = head.Hash()
			} else {
				return fmt.Errorf("resolve base ref %q: %w", base, err)
			}
		} else {
			baseHash = baseRef.Hash()
		}
	} else {
		baseHash = baseRef.Hash()
	}

	// Create branch reference.
	newRef := plumbing.NewHashReference(plumbing.NewBranchReferenceName(branch), baseHash)
	err = r.Storer.SetReference(newRef)
	if err != nil {
		return fmt.Errorf("create branch %q: %w", branch, err)
	}

	// Set HEAD to the new branch in the worktree.
	wt, err := r.Worktree()
	if err != nil {
		// Bare repo — branch created, nothing more to do.
		return nil
	}

	err = wt.Checkout(&gogit.CheckoutOptions{
		Branch: plumbing.NewBranchReferenceName(branch),
	})
	if err != nil {
		return fmt.Errorf("checkout branch %q: %w", branch, err)
	}

	g.logger.Info("branch created", zap.String("branch", branch), zap.String("base", base))
	return nil
}

// CommitPaths validates paths, stages them, and creates a commit.
func (g *gitClient) CommitPaths(ctx context.Context, repo string, cs ChangeSet) (Commit, error) {
	// Local-only operation: go-git does not accept a context here.
	_, span := g.tracer.StartSpan(ctx, "git.CommitPaths")
	defer span.End()

	r, err := gogit.PlainOpen(repo)
	if err != nil {
		return Commit{}, fmt.Errorf("open repo: %w", err)
	}

	wt, err := r.Worktree()
	if err != nil {
		return Commit{}, fmt.Errorf("get worktree: %w", err)
	}

	// Resolve the absolute repo root for path validation.
	absRoot, err := filepath.Abs(repo)
	if err != nil {
		return Commit{}, fmt.Errorf("resolve repo root: %w", err)
	}
	absRoot = filepath.Clean(absRoot)

	// Validate and stage each path.
	for _, p := range cs.Paths {
		absPath := filepath.Join(absRoot, p)
		absPath = filepath.Clean(absPath)

		// Security check: the path must resolve inside the repo root.
		if !strings.HasPrefix(absPath, absRoot+string(filepath.Separator)) && absPath != absRoot {
			return Commit{}, &ErrPathOutsideRepo{Path: p}
		}

		// Stage the file. Use the repo-relative path for go-git.
		_, err = wt.Add(p)
		if err != nil {
			return Commit{}, fmt.Errorf("stage path %q: %w", p, err)
		}
	}

	// Create commit.
	commitHash, err := wt.Commit(cs.Message, &gogit.CommitOptions{
		Author: &object.Signature{
			Name:  cs.Author.Name,
			Email: cs.Author.Email,
			When:  time.Now().UTC(),
		},
	})
	if err != nil {
		return Commit{}, fmt.Errorf("commit: %w", err)
	}

	g.logger.Info("commit created",
		zap.String("sha", commitHash.String()),
		zap.String("message", cs.Message),
	)

	return Commit{
		SHA:     commitHash.String(),
		Message: cs.Message,
	}, nil
}

// Push pushes the specified branch to the remote. Force-push is never supported.
func (g *gitClient) Push(ctx context.Context, repo, branch string) error {
	ctx, span := g.tracer.StartSpan(ctx, "git.Push")
	defer span.End()

	r, err := gogit.PlainOpen(repo)
	if err != nil {
		return fmt.Errorf("open repo: %w", err)
	}

	token, err := g.tokens.Token(ctx)
	if err != nil {
		return err
	}

	// Pre-push fast-forward check: fetch remote ref and verify local is descendant.
	if err := g.checkFastForward(r, branch); err != nil {
		return err
	}

	refSpec := gogitconfig.RefSpec(fmt.Sprintf("refs/heads/%s:refs/heads/%s", branch, branch))
	err = r.PushContext(ctx, &gogit.PushOptions{
		RemoteName: "origin",
		RefSpecs:   []gogitconfig.RefSpec{refSpec},
		Auth: &gogithttp.BasicAuth{
			Username: "x-access-token",
			Password: token,
		},
		Force: false, // Never force push.
	})
	if err != nil {
		if err == gogit.NoErrAlreadyUpToDate {
			return nil
		}
		if errors.Is(err, gogit.ErrNonFastForwardUpdate) {
			return &ErrPushRejected{Branch: branch, Reason: err.Error()}
		}
		errMsg := err.Error()
		if strings.Contains(errMsg, "non-fast-forward") || strings.Contains(errMsg, "rejected") {
			return &ErrPushRejected{Branch: branch, Reason: errMsg}
		}
		if strings.Contains(errMsg, "authentication") || strings.Contains(errMsg, "authorization") ||
			strings.Contains(errMsg, "401") || strings.Contains(errMsg, "403") {
			return &ErrGitAuth{Reason: errMsg}
		}
		return fmt.Errorf("push branch %q: %w", branch, err)
	}

	g.logger.Info("push completed", zap.String("branch", branch))
	return nil
}

// checkFastForward verifies that a push would be a fast-forward update.
// It fetches the current remote ref and checks that it is an ancestor of the
// local branch tip. Returns ErrPushRejected if not.
func (g *gitClient) checkFastForward(r *gogit.Repository, branch string) error {
	// Get local branch hash.
	localRef, err := r.Reference(plumbing.NewBranchReferenceName(branch), true)
	if err != nil {
		return fmt.Errorf("resolve local branch %q: %w", branch, err)
	}
	localHash := localRef.Hash()

	// Fetch remote refs to update our knowledge of remote state.
	err = r.Fetch(&gogit.FetchOptions{
		RemoteName: "origin",
		RefSpecs:   []gogitconfig.RefSpec{gogitconfig.RefSpec("+refs/heads/*:refs/remotes/origin/*")},
		Force:      true,
	})
	if err != nil && err != gogit.NoErrAlreadyUpToDate {
		// Can't fetch — skip pre-check, let the actual push decide.
		return nil
	}

	// Check the remote tracking ref for the branch.
	remoteRef, err := r.Reference(plumbing.NewRemoteReferenceName("origin", branch), true)
	if err != nil {
		// Remote doesn't have the branch yet — always fast-forward.
		return nil
	}
	remoteHash := remoteRef.Hash()

	// If they're the same, nothing to do.
	if remoteHash == localHash {
		return nil
	}

	// Check if remoteHash is an ancestor of localHash.
	isAnc, err := isAncestorOf(r, remoteHash, localHash)
	if err != nil {
		// If we can't determine ancestry, let the push proceed.
		return nil
	}

	if !isAnc {
		return &ErrPushRejected{
			Branch: branch,
			Reason: "non-fast-forward update: remote has diverged",
		}
	}

	return nil
}

// isAncestorOf checks if ancestor is an ancestor of descendant by walking
// the commit history.
func isAncestorOf(r *gogit.Repository, ancestor, descendant plumbing.Hash) (bool, error) {
	// Walk from descendant backwards looking for ancestor.
	seen := make(map[plumbing.Hash]bool)
	queue := []plumbing.Hash{descendant}

	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]

		if current == ancestor {
			return true, nil
		}

		if seen[current] {
			continue
		}
		seen[current] = true

		commit, err := r.CommitObject(current)
		if err != nil {
			return false, err
		}

		for _, parent := range commit.ParentHashes {
			if !seen[parent] {
				queue = append(queue, parent)
			}
		}
	}

	return false, nil
}

// OpenPullRequest creates a pull request using the GitHub API.
func (g *gitClient) OpenPullRequest(ctx context.Context, req PullRequestRequest) (PullRequest, error) {
	ctx, span := g.tracer.StartSpan(ctx, "git.OpenPullRequest")
	defer span.End()

	client := g.githubClient(ctx)

	pr, resp, err := client.PullRequests.Create(ctx, req.Owner, req.Repo, &github.NewPullRequest{
		Title: github.Ptr(req.Title),
		Body:  github.Ptr(req.Body),
		Head:  github.Ptr(req.Head),
		Base:  github.Ptr(req.Base),
	})
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusForbidden {
			resetAt := parseRateLimitReset(resp)
			return PullRequest{}, &ErrRateLimited{ResetAt: resetAt}
		}
		return PullRequest{}, fmt.Errorf("create PR: %w", err)
	}

	g.logger.Info("PR created",
		zap.Int("number", pr.GetNumber()),
		zap.String("url", pr.GetHTMLURL()),
	)

	return PullRequest{
		Number: pr.GetNumber(),
		URL:    pr.GetHTMLURL(),
		State:  pr.GetState(),
	}, nil
}

// PullRequestStatus retrieves the current status of a pull request.
func (g *gitClient) PullRequestStatus(ctx context.Context, owner, name string, number int) (PRStatus, error) {
	ctx, span := g.tracer.StartSpan(ctx, "git.PullRequestStatus")
	defer span.End()

	client := g.githubClient(ctx)

	pr, resp, err := client.PullRequests.Get(ctx, owner, name, number)
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusForbidden {
			resetAt := parseRateLimitReset(resp)
			return PRStatus{}, &ErrRateLimited{ResetAt: resetAt}
		}
		return PRStatus{}, fmt.Errorf("get PR #%d: %w", number, err)
	}

	state := pr.GetState()
	if pr.GetMerged() {
		state = "merged"
	}

	// Get review decision from reviews.
	reviewDecision := "pending"
	reviews, _, err := client.PullRequests.ListReviews(ctx, owner, name, number, nil)
	if err == nil && len(reviews) > 0 {
		// Use the latest review state.
		for i := len(reviews) - 1; i >= 0; i-- {
			rs := strings.ToLower(reviews[i].GetState())
			if rs == "approved" || rs == "changes_requested" {
				reviewDecision = rs
				break
			}
		}
	}

	var mergeable *bool
	if pr.Mergeable != nil {
		mergeable = pr.Mergeable
	}

	return PRStatus{
		Number:         pr.GetNumber(),
		State:          state,
		ReviewDecision: reviewDecision,
		Mergeable:      mergeable,
		HeadSHA:        pr.GetHead().GetSHA(),
		UpdatedAt:      pr.GetUpdatedAt().Time,
	}, nil
}

// PollUntil polls the PR status at the given interval until a terminal state is
// reached or the timeout expires. Terminal states: approved, changes_requested,
// closed, merged. Returns last observed status on timeout.
func (g *gitClient) PollUntil(ctx context.Context, owner, name string, number int, interval, timeout time.Duration) (PRStatus, error) {
	ctx, span := g.tracer.StartSpan(ctx, "git.PollUntil")
	defer span.End()

	deadline := time.After(timeout)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	var lastStatus PRStatus

	for {
		select {
		case <-ctx.Done():
			return lastStatus, ctx.Err()
		case <-deadline:
			return lastStatus, nil
		case <-ticker.C:
			status, err := g.PullRequestStatus(ctx, owner, name, number)
			if err != nil {
				return lastStatus, err
			}
			lastStatus = status

			// Check terminal states.
			if isTerminalState(status) {
				return status, nil
			}
		}
	}
}

// isTerminalState returns true if the PR is in a terminal state.
func isTerminalState(s PRStatus) bool {
	switch s.State {
	case "closed", "merged":
		return true
	}
	switch s.ReviewDecision {
	case "approved", "changes_requested":
		return true
	}
	return false
}

// githubClient constructs a go-github client using the configured base URL.
func (g *gitClient) githubClient(ctx context.Context) *github.Client {
	token, _ := g.tokens.Token(ctx)

	httpClient := g.httpClient
	if httpClient == nil {
		httpClient = http.DefaultClient
	}

	var client *github.Client
	if g.cfg.GitHubAPIBaseURL != "" {
		client, _ = github.NewClient(httpClient).WithAuthToken(token).WithEnterpriseURLs(g.cfg.GitHubAPIBaseURL, g.cfg.GitHubAPIBaseURL)
	} else {
		client = github.NewClient(httpClient).WithAuthToken(token)
	}

	return client
}

// parseRateLimitReset extracts the rate limit reset time from a GitHub response.
func parseRateLimitReset(resp *github.Response) time.Time {
	if resp == nil || resp.Response == nil {
		return time.Now().Add(60 * time.Second)
	}
	resetStr := resp.Response.Header.Get("X-RateLimit-Reset")
	if resetStr != "" {
		if epoch, err := strconv.ParseInt(resetStr, 10, 64); err == nil {
			return time.Unix(epoch, 0)
		}
	}
	return time.Now().Add(60 * time.Second)
}
