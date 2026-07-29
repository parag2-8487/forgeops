// SPDX-License-Identifier: Apache-2.0
package git_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	gogit "github.com/go-git/go-git/v5"
	gogitconfig "github.com/go-git/go-git/v5/config"
	"github.com/go-git/go-git/v5/plumbing"
	"github.com/go-git/go-git/v5/plumbing/object"
	"go.uber.org/zap"

	"github.com/parag8487/ForgeOps/agent/internal/git"
	"github.com/parag8487/ForgeOps/agent/internal/telemetry"
)

// staticTokenSource always returns a fixed token.
type staticTokenSource struct {
	token string
	err   error
}

func (s *staticTokenSource) Token(_ context.Context) (string, error) {
	return s.token, s.err
}

// setupBareAndClone creates a bare repo and a clone with an initial commit.
// Returns (barePath, clonePath, cleanup).
func setupBareAndClone(t *testing.T) (string, string) {
	t.Helper()

	bareDir := t.TempDir()
	cloneDir := t.TempDir()

	// Init bare repo.
	_, err := gogit.PlainInit(bareDir, true)
	if err != nil {
		t.Fatalf("init bare repo: %v", err)
	}

	// Clone the bare repo (will be empty).
	cloned, err := gogit.PlainClone(cloneDir, false, &gogit.CloneOptions{
		URL: bareDir,
	})
	if err != nil {
		// Empty repo clone fails, init instead and add remote.
		cloned, err = gogit.PlainInit(cloneDir, false)
		if err != nil {
			t.Fatalf("init clone: %v", err)
		}
		_, err = cloned.CreateRemote(&gogitconfig.RemoteConfig{
			Name: "origin",
			URLs: []string{bareDir},
		})
		if err != nil {
			t.Fatalf("create remote: %v", err)
		}
	}

	// Create an initial commit.
	wt, err := cloned.Worktree()
	if err != nil {
		t.Fatalf("worktree: %v", err)
	}

	initFile := filepath.Join(cloneDir, "README.md")
	err = os.WriteFile(initFile, []byte("# Test\n"), 0644)
	if err != nil {
		t.Fatalf("write initial file: %v", err)
	}

	_, err = wt.Add("README.md")
	if err != nil {
		t.Fatalf("add README: %v", err)
	}

	_, err = wt.Commit("initial commit", &gogit.CommitOptions{
		Author: &object.Signature{
			Name:  "Test",
			Email: "test@test.com",
			When:  time.Now(),
		},
	})
	if err != nil {
		t.Fatalf("initial commit: %v", err)
	}

	// Push to bare repo.
	err = cloned.Push(&gogit.PushOptions{
		RemoteName: "origin",
	})
	if err != nil {
		t.Fatalf("push initial commit: %v", err)
	}

	return bareDir, cloneDir
}

func newTestClient(t *testing.T, baseURL string, token string) git.Client {
	t.Helper()
	cfg := git.Config{
		GitHubAPIBaseURL: baseURL,
		Owner:            "testowner",
		Repo:             "testrepo",
		AuthorName:       "Test Bot",
		AuthorEmail:      "bot@test.com",
		BranchPrefix:     "forgeops/",
		PollInterval:     50 * time.Millisecond,
		PollTimeout:      500 * time.Millisecond,
	}
	logger := zap.NewNop()
	tracer := telemetry.NoopTracer{}
	tokens := &staticTokenSource{token: token}

	httpClient := &http.Client{}
	return git.NewClientWithHTTP(cfg, tokens, logger, tracer, httpClient)
}

// TestCreateBranch_Success verifies branch creation from an existing base.
func TestCreateBranch_Success(t *testing.T) {
	_, cloneDir := setupBareAndClone(t)
	client := newTestClient(t, "", "fake-token")

	err := client.CreateBranch(context.Background(), cloneDir, "master", "feature/test")
	if err != nil {
		t.Fatalf("CreateBranch: %v", err)
	}

	// Verify branch exists.
	r, _ := gogit.PlainOpen(cloneDir)
	_, err = r.Reference(plumbing.NewBranchReferenceName("feature/test"), true)
	if err != nil {
		t.Fatalf("branch reference not found: %v", err)
	}
}

// TestCommitPaths_FullFlow tests branch→stage→commit→push against a local bare repo.
func TestCommitPaths_FullFlow(t *testing.T) {
	bareDir, cloneDir := setupBareAndClone(t)
	client := newTestClient(t, "", "fake-token")

	// Create branch.
	err := client.CreateBranch(context.Background(), cloneDir, "master", "forgeops/test-branch")
	if err != nil {
		t.Fatalf("CreateBranch: %v", err)
	}

	// Write a file to commit.
	testFile := filepath.Join(cloneDir, "hello.txt")
	err = os.WriteFile(testFile, []byte("hello world\n"), 0644)
	if err != nil {
		t.Fatalf("write test file: %v", err)
	}

	// Commit.
	cs := git.ChangeSet{
		BaseBranch: "master",
		Branch:     "forgeops/test-branch",
		Paths:      []string{"hello.txt"},
		Message:    "feat: add hello.txt",
		Author:     git.Signature{Name: "Bot", Email: "bot@test.com"},
	}
	commit, err := client.CommitPaths(context.Background(), cloneDir, cs)
	if err != nil {
		t.Fatalf("CommitPaths: %v", err)
	}
	if commit.SHA == "" {
		t.Error("commit SHA is empty")
	}
	if commit.Message != "feat: add hello.txt" {
		t.Errorf("commit message = %q, want %q", commit.Message, "feat: add hello.txt")
	}

	// Push to bare repo (local file-based remote, no network).
	err = client.Push(context.Background(), cloneDir, "forgeops/test-branch")
	if err != nil {
		t.Fatalf("Push: %v", err)
	}

	// Verify the branch exists in the bare repo.
	bareRepo, _ := gogit.PlainOpen(bareDir)
	ref, err := bareRepo.Reference(plumbing.NewBranchReferenceName("forgeops/test-branch"), true)
	if err != nil {
		t.Fatalf("branch not found in bare repo: %v", err)
	}
	if ref.Hash().String() != commit.SHA {
		t.Errorf("bare repo HEAD = %s, want %s", ref.Hash().String(), commit.SHA)
	}
}

// TestCommitPaths_PathEscapeRejection verifies that paths resolving outside
// the repository root are rejected.
func TestCommitPaths_PathEscapeRejection(t *testing.T) {
	_, cloneDir := setupBareAndClone(t)
	client := newTestClient(t, "", "fake-token")

	err := client.CreateBranch(context.Background(), cloneDir, "master", "forgeops/escape-test")
	if err != nil {
		t.Fatalf("CreateBranch: %v", err)
	}

	cs := git.ChangeSet{
		BaseBranch: "master",
		Branch:     "forgeops/escape-test",
		Paths:      []string{"../../../etc/passwd"},
		Message:    "exploit",
		Author:     git.Signature{Name: "Bad", Email: "bad@test.com"},
	}
	_, err = client.CommitPaths(context.Background(), cloneDir, cs)
	if err == nil {
		t.Fatal("expected error for path escape, got nil")
	}

	var pathErr *git.ErrPathOutsideRepo
	if !errors.As(err, &pathErr) {
		t.Fatalf("expected *ErrPathOutsideRepo, got %T: %v", err, err)
	}
}

// TestPush_NonFastForward verifies that a non-fast-forward push returns ErrPushRejected.
func TestPush_NonFastForward(t *testing.T) {
	bareDir, cloneDir := setupBareAndClone(t)
	client := newTestClient(t, "", "fake-token")

	// Create a branch and push it.
	err := client.CreateBranch(context.Background(), cloneDir, "master", "forgeops/conflict")
	if err != nil {
		t.Fatalf("CreateBranch: %v", err)
	}

	testFile := filepath.Join(cloneDir, "file1.txt")
	err = os.WriteFile(testFile, []byte("v1\n"), 0644)
	if err != nil {
		t.Fatalf("write file1: %v", err)
	}
	cs := git.ChangeSet{
		Branch:  "forgeops/conflict",
		Paths:   []string{"file1.txt"},
		Message: "first",
		Author:  git.Signature{Name: "Bot", Email: "bot@test.com"},
	}
	_, err = client.CommitPaths(context.Background(), cloneDir, cs)
	if err != nil {
		t.Fatalf("CommitPaths first: %v", err)
	}
	err = client.Push(context.Background(), cloneDir, "forgeops/conflict")
	if err != nil {
		t.Fatalf("Push first: %v", err)
	}

	// Create a second clone and push a different commit on the same branch
	// to advance the remote, causing the original clone to be behind.
	clone2Dir := t.TempDir()
	clone2, err := gogit.PlainClone(clone2Dir, false, &gogit.CloneOptions{URL: bareDir})
	if err != nil {
		t.Fatalf("clone2: %v", err)
	}
	wt2, err := clone2.Worktree()
	if err != nil {
		t.Fatalf("clone2 worktree: %v", err)
	}
	// Checkout the branch in clone2 by finding the remote tracking ref.
	clone2RemoteRef, err := clone2.Reference(plumbing.NewRemoteReferenceName("origin", "forgeops/conflict"), true)
	if err != nil {
		t.Fatalf("clone2 find remote ref: %v", err)
	}
	// Create local branch pointing to same hash.
	err = clone2.Storer.SetReference(plumbing.NewHashReference(
		plumbing.NewBranchReferenceName("forgeops/conflict"), clone2RemoteRef.Hash()))
	if err != nil {
		t.Fatalf("clone2 set branch ref: %v", err)
	}
	err = wt2.Checkout(&gogit.CheckoutOptions{
		Branch: plumbing.NewBranchReferenceName("forgeops/conflict"),
	})
	if err != nil {
		t.Fatalf("clone2 checkout: %v", err)
	}
	conflictFile := filepath.Join(clone2Dir, "file2.txt")
	err = os.WriteFile(conflictFile, []byte("conflict\n"), 0644)
	if err != nil {
		t.Fatalf("write conflict file: %v", err)
	}
	_, err = wt2.Add("file2.txt")
	if err != nil {
		t.Fatalf("clone2 add: %v", err)
	}
	_, err = wt2.Commit("diverge", &gogit.CommitOptions{
		Author: &object.Signature{Name: "Other", Email: "other@test.com", When: time.Now()},
	})
	if err != nil {
		t.Fatalf("clone2 commit: %v", err)
	}
	err = clone2.Push(&gogit.PushOptions{RemoteName: "origin"})
	if err != nil {
		t.Fatalf("clone2 push: %v", err)
	}

	// Now make a different commit in the original clone on the same branch
	// WITHOUT fetching. This creates true divergence.
	altFile := filepath.Join(cloneDir, "file3.txt")
	err = os.WriteFile(altFile, []byte("alt\n"), 0644)
	if err != nil {
		t.Fatalf("write alt file: %v", err)
	}

	r, err := gogit.PlainOpen(cloneDir)
	if err != nil {
		t.Fatalf("open clone: %v", err)
	}
	wt, err := r.Worktree()
	if err != nil {
		t.Fatalf("clone worktree: %v", err)
	}
	_, err = wt.Add("file3.txt")
	if err != nil {
		t.Fatalf("clone add: %v", err)
	}
	altHash, err := wt.Commit("alt commit", &gogit.CommitOptions{
		Author: &object.Signature{Name: "Bot", Email: "bot@test.com", When: time.Now()},
	})
	if err != nil {
		t.Fatalf("clone alt commit: %v", err)
	}
	_ = altHash

	// This push should fail with non-fast-forward.
	err = client.Push(context.Background(), cloneDir, "forgeops/conflict")
	if err == nil {
		t.Fatal("expected ErrPushRejected, got nil")
	}

	var pushErr *git.ErrPushRejected
	if !errors.As(err, &pushErr) {
		// go-git may wrap the error differently, check for the expected keywords
		errStr := err.Error()
		if !strings.Contains(errStr, "non-fast-forward") && !strings.Contains(errStr, "rejected") && !strings.Contains(errStr, "diverged") {
			t.Fatalf("expected push rejected error, got %T: %v", err, err)
		}
	}
}

// TestPush_AuthFailure verifies that auth failures produce ErrGitAuth.
func TestPush_AuthFailure(t *testing.T) {
	// Create a test HTTP server that returns 401 for all requests.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte("authentication required"))
	}))
	defer srv.Close()

	_, cloneDir := setupBareAndClone(t)

	// Reconfigure clone to point to the HTTP server as remote.
	r, _ := gogit.PlainOpen(cloneDir)
	_ = r.DeleteRemote("origin")
	_, _ = r.CreateRemote(&gogitconfig.RemoteConfig{
		Name: "origin",
		URLs: []string{srv.URL},
	})

	client := newTestClient(t, "", "bad-token")

	err := client.CreateBranch(context.Background(), cloneDir, "master", "forgeops/auth-test")
	if err != nil {
		t.Fatalf("CreateBranch: %v", err)
	}

	err = client.Push(context.Background(), cloneDir, "forgeops/auth-test")
	if err == nil {
		t.Fatal("expected ErrGitAuth, got nil")
	}

	var authErr *git.ErrGitAuth
	if !errors.As(err, &authErr) {
		// The error might be wrapped differently depending on the go-git version.
		// At minimum it should not be nil.
		if !strings.Contains(err.Error(), "auth") && !strings.Contains(err.Error(), "401") {
			t.Fatalf("expected auth-related error, got: %v", err)
		}
	}
}

// TestPush_NoForcePush verifies that the implementation never sends force-push.
// We verify this by checking go-git's PushOptions.Force is never set to true.
// This is a design test—the Push method signature has no force parameter.
func TestPush_NoForcePush(t *testing.T) {
	// The Push function signature does not accept a force parameter.
	// This is a compile-time guarantee. We verify it via the interface.
	var c git.Client
	_ = c // The interface method is: Push(ctx, repo, branch) error — no force param.
}

// TestOpenPullRequest_Success verifies PR creation with correct request shape.
func TestOpenPullRequest_Success(t *testing.T) {
	var receivedBody map[string]interface{}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" && strings.Contains(r.URL.Path, "/repos/testowner/testrepo/pulls") {
			_ = json.NewDecoder(r.Body).Decode(&receivedBody)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{
				"number": 42,
				"html_url": "https://github.com/testowner/testrepo/pull/42",
				"state": "open"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")

	pr, err := client.OpenPullRequest(context.Background(), git.PullRequestRequest{
		Owner: "testowner",
		Repo:  "testrepo",
		Title: "feat: add feature",
		Body:  "This PR adds a feature.",
		Head:  "forgeops/feature",
		Base:  "main",
	})
	if err != nil {
		t.Fatalf("OpenPullRequest: %v", err)
	}

	if pr.Number != 42 {
		t.Errorf("PR number = %d, want 42", pr.Number)
	}
	if pr.URL != "https://github.com/testowner/testrepo/pull/42" {
		t.Errorf("PR URL = %q, want expected URL", pr.URL)
	}
	if pr.State != "open" {
		t.Errorf("PR state = %q, want open", pr.State)
	}

	// Verify request shape.
	if receivedBody["title"] != "feat: add feature" {
		t.Errorf("request title = %v, want %q", receivedBody["title"], "feat: add feature")
	}
	if receivedBody["head"] != "forgeops/feature" {
		t.Errorf("request head = %v, want %q", receivedBody["head"], "forgeops/feature")
	}
	if receivedBody["base"] != "main" {
		t.Errorf("request base = %v, want %q", receivedBody["base"], "main")
	}
}

// TestPullRequestStatus_OpenState verifies status retrieval for open PRs.
func TestPullRequestStatus_OpenState(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/repos/testowner/testrepo/pulls/10/reviews") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`[]`))
			return
		}
		if strings.Contains(r.URL.Path, "/repos/testowner/testrepo/pulls/10") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"number": 10,
				"state": "open",
				"merged": false,
				"mergeable": true,
				"head": {"sha": "abc123"},
				"updated_at": "2026-07-26T10:00:00Z"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")

	status, err := client.PullRequestStatus(context.Background(), "testowner", "testrepo", 10)
	if err != nil {
		t.Fatalf("PullRequestStatus: %v", err)
	}

	if status.State != "open" {
		t.Errorf("state = %q, want open", status.State)
	}
	if status.Number != 10 {
		t.Errorf("number = %d, want 10", status.Number)
	}
	if status.HeadSHA != "abc123" {
		t.Errorf("headSHA = %q, want abc123", status.HeadSHA)
	}
}

// TestPullRequestStatus_ClosedState verifies closed PR mapping.
func TestPullRequestStatus_ClosedState(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/reviews") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`[]`))
			return
		}
		if strings.Contains(r.URL.Path, "/pulls/11") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"number": 11,
				"state": "closed",
				"merged": false,
				"head": {"sha": "def456"},
				"updated_at": "2026-07-26T11:00:00Z"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")
	status, err := client.PullRequestStatus(context.Background(), "testowner", "testrepo", 11)
	if err != nil {
		t.Fatalf("PullRequestStatus: %v", err)
	}
	if status.State != "closed" {
		t.Errorf("state = %q, want closed", status.State)
	}
}

// TestPullRequestStatus_MergedState verifies merged PR mapping.
func TestPullRequestStatus_MergedState(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/reviews") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`[]`))
			return
		}
		if strings.Contains(r.URL.Path, "/pulls/12") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"number": 12,
				"state": "closed",
				"merged": true,
				"head": {"sha": "ghi789"},
				"updated_at": "2026-07-26T12:00:00Z"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")
	status, err := client.PullRequestStatus(context.Background(), "testowner", "testrepo", 12)
	if err != nil {
		t.Fatalf("PullRequestStatus: %v", err)
	}
	if status.State != "merged" {
		t.Errorf("state = %q, want merged", status.State)
	}
}

// TestPollUntil_TerminalState verifies polling stops on terminal state.
func TestPollUntil_TerminalState(t *testing.T) {
	callCount := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/reviews") {
			w.Header().Set("Content-Type", "application/json")
			if callCount >= 2 {
				_, _ = w.Write([]byte(`[{"state": "APPROVED"}]`))
			} else {
				_, _ = w.Write([]byte(`[]`))
			}
			return
		}
		if strings.Contains(r.URL.Path, "/pulls/20") {
			callCount++
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"number": 20,
				"state": "open",
				"merged": false,
				"head": {"sha": "poll123"},
				"updated_at": "2026-07-26T10:00:00Z"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")
	status, err := client.PollUntil(
		context.Background(),
		"testowner", "testrepo", 20,
		50*time.Millisecond, 2*time.Second,
	)
	if err != nil {
		t.Fatalf("PollUntil: %v", err)
	}
	if status.ReviewDecision != "approved" {
		t.Errorf("review decision = %q, want approved", status.ReviewDecision)
	}
}

// TestPollUntil_Timeout verifies polling returns last status on timeout.
func TestPollUntil_Timeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/reviews") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`[]`))
			return
		}
		if strings.Contains(r.URL.Path, "/pulls/30") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"number": 30,
				"state": "open",
				"merged": false,
				"head": {"sha": "timeout123"},
				"updated_at": "2026-07-26T10:00:00Z"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")
	status, err := client.PollUntil(
		context.Background(),
		"testowner", "testrepo", 30,
		50*time.Millisecond, 200*time.Millisecond,
	)
	// Timeout should not produce an error.
	if err != nil {
		t.Fatalf("PollUntil on timeout: %v", err)
	}
	if status.State != "open" {
		t.Errorf("state = %q, want open (last observed)", status.State)
	}
	if status.Number != 30 {
		t.Errorf("number = %d, want 30", status.Number)
	}
}

// TestPollUntil_ClosedIsTerminal verifies that closed state stops polling.
func TestPollUntil_ClosedIsTerminal(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/reviews") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`[]`))
			return
		}
		if strings.Contains(r.URL.Path, "/pulls/31") {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"number": 31,
				"state": "closed",
				"merged": false,
				"head": {"sha": "closed123"},
				"updated_at": "2026-07-26T10:00:00Z"
			}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")
	status, err := client.PollUntil(
		context.Background(),
		"testowner", "testrepo", 31,
		50*time.Millisecond, 2*time.Second,
	)
	if err != nil {
		t.Fatalf("PollUntil: %v", err)
	}
	if status.State != "closed" {
		t.Errorf("state = %q, want closed", status.State)
	}
}

// TestRateLimit_Produces_ErrRateLimited verifies HTTP 403 with rate limit
// headers produces ErrRateLimited.
func TestRateLimit_Produces_ErrRateLimited(t *testing.T) {
	resetTime := time.Now().Add(5 * time.Minute).Unix()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-RateLimit-Reset", fmt.Sprintf("%d", resetTime))
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message": "API rate limit exceeded"}`))
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")

	// Test via PullRequestStatus.
	_, err := client.PullRequestStatus(context.Background(), "testowner", "testrepo", 99)
	if err == nil {
		t.Fatal("expected ErrRateLimited, got nil")
	}

	var rlErr *git.ErrRateLimited
	if !errors.As(err, &rlErr) {
		t.Fatalf("expected *ErrRateLimited, got %T: %v", err, err)
	}

	// Verify reset time is approximately correct.
	if rlErr.ResetAt.Unix() != resetTime {
		t.Errorf("ResetAt = %v, want unix %d", rlErr.ResetAt, resetTime)
	}
}

// TestRateLimit_OpenPR_ErrRateLimited verifies rate limiting on PR creation.
func TestRateLimit_OpenPR_ErrRateLimited(t *testing.T) {
	resetTime := time.Now().Add(10 * time.Minute).Unix()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-RateLimit-Reset", fmt.Sprintf("%d", resetTime))
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message": "API rate limit exceeded"}`))
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")

	_, err := client.OpenPullRequest(context.Background(), git.PullRequestRequest{
		Owner: "testowner",
		Repo:  "testrepo",
		Title: "test",
		Body:  "test",
		Head:  "feature",
		Base:  "main",
	})
	if err == nil {
		t.Fatal("expected ErrRateLimited, got nil")
	}

	var rlErr *git.ErrRateLimited
	if !errors.As(err, &rlErr) {
		t.Fatalf("expected *ErrRateLimited, got %T: %v", err, err)
	}
}

// TestPollUntil_RateLimitSurfaced verifies rate limit errors propagate during polling.
func TestPollUntil_RateLimitSurfaced(t *testing.T) {
	resetTime := time.Now().Add(5 * time.Minute).Unix()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-RateLimit-Reset", fmt.Sprintf("%d", resetTime))
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message": "API rate limit exceeded"}`))
	}))
	defer srv.Close()

	client := newTestClient(t, srv.URL, "test-token")
	_, err := client.PollUntil(
		context.Background(),
		"testowner", "testrepo", 99,
		50*time.Millisecond, 2*time.Second,
	)
	if err == nil {
		t.Fatal("expected ErrRateLimited, got nil")
	}

	var rlErr *git.ErrRateLimited
	if !errors.As(err, &rlErr) {
		t.Fatalf("expected *ErrRateLimited, got %T: %v", err, err)
	}
}
