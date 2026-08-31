// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/git"
)

// The last four catalogued operations: the workspace registry and the two forge operations.
//
// These were the remaining `unimplemented(...)` rows, and the ones the dispatcher's own comment
// described as arriving with "group 12's workspace registry" and "the git operations, which wrap Phase 0's
// client unchanged". That second phrase was accurate — `internal/git` is a complete client with
// `CreateBranch`, `CommitPaths`, `Push`, `OpenPullRequest` and `PullRequestStatus`, none of which had a
// caller. Wrapping it is most of the work here, and the wrapping is where the interesting decisions are.

// GitClient is the forge client, declared by its consumer.
//
// An interface here rather than an import of the concrete type, for the same reason `CodebaseIndexer` and
// `PolicySource` are: it keeps an HTTP client and a token source out of every `executor` test binary, and
// it makes the dependency visible in `Deps` rather than constructed in a handler.
type GitClient interface {
	CreateBranch(ctx context.Context, repo string, base, branch string) error
	CommitPaths(ctx context.Context, repo string, cs git.ChangeSet) (git.Commit, error)
	Push(ctx context.Context, repo, branch string) error
	OpenPullRequest(ctx context.Context, req git.PullRequestRequest) (git.PullRequest, error)
}

// ErrNoGitClient is the refusal when a forge operation is asked of an agent with no client wired.
//
// A NAMED REFUSAL RATHER THAN A SILENT SUCCESS, on the same argument as `ErrNoIndexer`: reporting success
// for a push that never happened would have the backend record a change set as delivered and an operator
// look for a branch that does not exist.
var ErrNoGitClient = errors.New("executor: no git client is wired")

// ── the workspace registry ───────────────────────────────────────────────────────────────────────

// projectRegistryArgs names the project a registration concerns.
//
// No path. The workspace comes from the agent's own configuration for the same reason `scanArgs` omits it:
// a root in a signed envelope would let the sender relocate what gets read and written.
type projectRegistryArgs struct {
	ProjectID string `json:"project_id"`
}

func decodeRegistryArgs(v *envelope.Verified) (projectRegistryArgs, error) {
	var args projectRegistryArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return projectRegistryArgs{}, fmt.Errorf("executor: undecodable registry arguments: %w", err)
	}
	if strings.TrimSpace(args.ProjectID) == "" {
		return projectRegistryArgs{}, errors.New("executor: a registration needs a project_id")
	}
	return args, nil
}

// ProjectRegistration is what the registry operations report.
type ProjectRegistration struct {
	ProjectID string `json:"project_id"`
	Workspace string `json:"workspace"`
	//: Every project this agent currently serves, sorted. Reported so a backend can reconcile its own
	//: view against the agent's rather than assuming the two agree.
	Registered []string `json:"registered"`
	//: True when this call changed the set. An idempotent re-registration is a success and says so.
	Changed bool `json:"changed"`
}

// projectRegister records that this agent serves a project.
//
// READ-ONLY DESPITE THE NAME. It writes nothing to the workspace and creates nothing on the machine: the
// registry is the agent's in-memory statement of which projects it will accept commands for, and the
// authoritative record lives in `agent_devices` on the backend. Marking it mutating would require an
// approval for an operator to connect a workspace they already own.
func projectRegister(_ context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	args, err := decodeRegistryArgs(v)
	if err != nil {
		return Result{}, err
	}
	changed := d.projects.add(args.ProjectID)
	report := ProjectRegistration{
		ProjectID:  args.ProjectID,
		Workspace:  d.root,
		Registered: d.projects.list(),
		Changed:    changed,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable registration: %w", err)
	}
	sink.Progress(100, "project.register", fmt.Sprintf(
		"%s is served from %s (%d project(s) registered)", args.ProjectID, d.root, len(report.Registered)))
	return Result{Status: "registered", Output: string(encoded)}, nil
}

// projectUnregister stops this agent serving a project.
//
// Unregistering a project that was never registered is a SUCCESS with `changed: false`, not an error. The
// caller's intent — "this agent should not serve that project" — is satisfied either way, and a backend
// reconciling after a restart would otherwise have to distinguish two states it does not care about.
func projectUnregister(_ context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	args, err := decodeRegistryArgs(v)
	if err != nil {
		return Result{}, err
	}
	changed := d.projects.remove(args.ProjectID)
	// The project's injected secrets go with it. Leaving them would keep credentials in memory for a
	// project this agent has been told to stop serving, which is exactly the lifetime FR-45 avoids.
	d.secrets.forget(args.ProjectID)
	report := ProjectRegistration{
		ProjectID:  args.ProjectID,
		Workspace:  d.root,
		Registered: d.projects.list(),
		Changed:    changed,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable registration: %w", err)
	}
	sink.Progress(100, "project.unregister", fmt.Sprintf(
		"%s is no longer served (%d project(s) registered)", args.ProjectID, len(report.Registered)))
	return Result{Status: "unregistered", Output: string(encoded)}, nil
}

// ── the forge operations ─────────────────────────────────────────────────────────────────────────

// gitPushArgs is the argument object for `git.branch_commit_push`.
type gitPushArgs struct {
	Repo       string   `json:"repo"`
	BaseBranch string   `json:"base_branch"`
	Branch     string   `json:"branch"`
	Paths      []string `json:"paths"`
	Message    string   `json:"message"`
	AuthorName string   `json:"author_name"`
	AuthorMail string   `json:"author_email"`
}

// GitPushReport is what a successful branch-commit-push reports.
type GitPushReport struct {
	Branch    string `json:"branch"`
	CommitSHA string `json:"commit_sha"`
	Message   string `json:"message"`
	Paths     int    `json:"paths"`
}

func gitBranchCommitPush(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	if d.gitClient == nil {
		return Result{}, fmt.Errorf("%w: git.branch_commit_push cannot run", ErrNoGitClient)
	}
	var args gitPushArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("executor: undecodable git arguments: %w", err)
	}
	switch {
	case strings.TrimSpace(args.Branch) == "":
		return Result{}, errors.New("executor: git.branch_commit_push needs a branch")
	case strings.TrimSpace(args.Message) == "":
		// An empty commit message produces a commit nobody can interpret later, and the audit trail
		// records the operation rather than the diff.
		return Result{}, errors.New("executor: git.branch_commit_push needs a commit message")
	case len(args.Paths) == 0:
		return Result{}, errors.New("executor: git.branch_commit_push was given no paths to commit")
	}
	// The repository is the agent's own workspace. A repo path in the envelope would let the sender
	// commit from somewhere else on the machine, which is the same escape `fileops` confinement blocks
	// for reads and writes.
	repo := d.root
	base := args.BaseBranch
	if strings.TrimSpace(base) == "" {
		base = "main"
	}

	sink.Progress(15, "git.branch_commit_push", "creating "+args.Branch)
	if err := d.gitClient.CreateBranch(ctx, repo, base, args.Branch); err != nil {
		return Result{}, fmt.Errorf("executor: cannot create %s from %s: %w", args.Branch, base, err)
	}

	sink.Progress(45, "git.branch_commit_push", fmt.Sprintf("committing %d path(s)", len(args.Paths)))
	commit, err := d.gitClient.CommitPaths(ctx, repo, git.ChangeSet{
		BaseBranch: base,
		Branch:     args.Branch,
		Paths:      args.Paths,
		Message:    args.Message,
		Author:     git.Signature{Name: args.AuthorName, Email: args.AuthorMail},
	})
	if err != nil {
		return Result{}, fmt.Errorf("executor: commit failed: %w", err)
	}

	sink.Progress(80, "git.branch_commit_push", "pushing "+args.Branch)
	if err := d.gitClient.Push(ctx, repo, args.Branch); err != nil {
		// The commit exists locally and the push did not happen. Reported as a failure rather than a
		// partial success, because a backend told "pushed" would look for a branch on the remote.
		return Result{}, fmt.Errorf("executor: push of %s failed after committing %s: %w",
			args.Branch, commit.SHA, err)
	}

	report := GitPushReport{
		Branch:    args.Branch,
		CommitSHA: commit.SHA,
		Message:   commit.Message,
		Paths:     len(args.Paths),
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable push report: %w", err)
	}
	sink.Progress(100, "git.branch_commit_push", "pushed "+commit.SHA)
	return Result{Status: "pushed", Output: string(encoded)}, nil
}

// gitPRArgs is the argument object for `git.open_pr`.
type gitPRArgs struct {
	Owner string `json:"owner"`
	Repo  string `json:"repo"`
	Title string `json:"title"`
	Body  string `json:"body"`
	Head  string `json:"head"`
	Base  string `json:"base"`
}

func gitOpenPR(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	if d.gitClient == nil {
		return Result{}, fmt.Errorf("%w: git.open_pr cannot run", ErrNoGitClient)
	}
	var args gitPRArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("executor: undecodable pull request arguments: %w", err)
	}
	for name, value := range map[string]string{
		"owner": args.Owner, "repo": args.Repo, "title": args.Title, "head": args.Head,
	} {
		if strings.TrimSpace(value) == "" {
			return Result{}, fmt.Errorf("executor: git.open_pr needs a %s", name)
		}
	}
	base := args.Base
	if strings.TrimSpace(base) == "" {
		base = "main"
	}

	sink.Progress(30, "git.open_pr", fmt.Sprintf("opening %s -> %s", args.Head, base))
	pr, err := d.gitClient.OpenPullRequest(ctx, git.PullRequestRequest{
		Owner: args.Owner,
		Repo:  args.Repo,
		Title: args.Title,
		Body:  args.Body,
		Head:  args.Head,
	})
	if err != nil {
		return Result{}, fmt.Errorf("executor: opening the pull request failed: %w", err)
	}
	encoded, err := json.Marshal(pr)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable pull request: %w", err)
	}
	sink.Progress(100, "git.open_pr", "opened")
	return Result{Status: "opened", Output: string(encoded)}, nil
}

// ── the registry's storage ───────────────────────────────────────────────────────────────────────

// projectRegistry is the set of projects this agent serves.
//
// In memory, for the same reason the injected secrets are: the authoritative record is `agent_devices` on
// the backend, and a registry that survived a restart would let an agent keep serving a project the
// backend has since detached it from.
type projectRegistry struct {
	mu    sync.Mutex
	known map[string]struct{}
}

func newProjectRegistry() *projectRegistry {
	return &projectRegistry{known: map[string]struct{}{}}
}

func (r *projectRegistry) add(projectID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, present := r.known[projectID]; present {
		return false
	}
	r.known[projectID] = struct{}{}
	return true
}

func (r *projectRegistry) remove(projectID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, present := r.known[projectID]; !present {
		return false
	}
	delete(r.known, projectID)
	return true
}

func (r *projectRegistry) list() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, 0, len(r.known))
	for id := range r.known {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}
