// SPDX-License-Identifier: Apache-2.0
package executor

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/git"
)

// The last four operations that were `unimplemented(...)`: the workspace registry and the two forge
// operations. `internal/git` was a complete client with no caller, so wrapping it is most of the work and
// the wrapping is where the decisions are.

type recordingGit struct {
	createdBranch string
	createdBase   string
	committed     git.ChangeSet
	pushedBranch  string
	openedPR      git.PullRequestRequest
	createErr     error
	commitErr     error
	pushErr       error
	prErr         error
	calls         []string
}

func (r *recordingGit) CreateBranch(_ context.Context, _ string, base, branch string) error {
	r.calls = append(r.calls, "CreateBranch")
	r.createdBase, r.createdBranch = base, branch
	return r.createErr
}

func (r *recordingGit) CommitPaths(_ context.Context, _ string, cs git.ChangeSet) (git.Commit, error) {
	r.calls = append(r.calls, "CommitPaths")
	r.committed = cs
	if r.commitErr != nil {
		return git.Commit{}, r.commitErr
	}
	return git.Commit{SHA: "abc1234", Message: cs.Message}, nil
}

func (r *recordingGit) Push(_ context.Context, _ string, branch string) error {
	r.calls = append(r.calls, "Push")
	r.pushedBranch = branch
	return r.pushErr
}

func (r *recordingGit) OpenPullRequest(_ context.Context, req git.PullRequestRequest) (git.PullRequest, error) {
	r.calls = append(r.calls, "OpenPullRequest")
	r.openedPR = req
	if r.prErr != nil {
		return git.PullRequest{}, r.prErr
	}
	return git.PullRequest{Number: 42, URL: "https://example.invalid/pr/42"}, nil
}

func dispatcherWithGit(t *testing.T, client GitClient) Dispatcher {
	t.Helper()
	d, err := New(Deps{Root: t.TempDir(), Git: client})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return d
}

// ── the registry ─────────────────────────────────────────────────────────────────────────────────

func TestProjectRegister_IsReadOnlyAndNeedsNoApproval(t *testing.T) {
	// Read-only despite the name: nothing is written to the workspace and nothing is created on the
	// machine. Requiring an approval would mean an operator needs one to connect a workspace they own.
	for _, op := range []Operation{OpProjectRegister, OpProjectUnregister} {
		row := handlerTable[op]
		if row.mutating || row.requiresApproval {
			t.Errorf("%q is mutating=%v requiresApproval=%v; both must be false",
				op, row.mutating, row.requiresApproval)
		}
	}
}

func TestProjectRegister_RecordsTheProjectAndReportsTheSet(t *testing.T) {
	root := t.TempDir()
	d, err := New(Deps{Root: root})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	before := countFiles(t, root)

	res, err := d.Execute(context.Background(),
		verified(t, OpProjectRegister, "", map[string]any{"project_id": "proj-b"}, 71), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "registered" {
		t.Fatalf("status %q", res.Status)
	}
	var report ProjectRegistration
	if err := json.Unmarshal([]byte(res.Output), &report); err != nil {
		t.Fatalf("output is not a registration: %v", err)
	}
	if !report.Changed {
		t.Error("a first registration reported no change")
	}
	if report.Workspace != root {
		t.Errorf("workspace %q, want %q", report.Workspace, root)
	}
	if after := countFiles(t, root); after != before {
		t.Error("registration wrote to the workspace")
	}

	// A second registration of the same project is a SUCCESS with changed=false. The caller's intent is
	// already satisfied, and a backend reconciling after a restart should not have to distinguish two
	// states it does not care about.
	res, err = d.Execute(context.Background(),
		verified(t, OpProjectRegister, "", map[string]any{"project_id": "proj-b"}, 72), nil)
	if err != nil {
		t.Fatalf("re-registering: %v", err)
	}
	_ = json.Unmarshal([]byte(res.Output), &report)
	if report.Changed {
		t.Error("re-registering the same project reported a change")
	}
}

func TestProjectRegister_TheReportedSetIsSortedAndComplete(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for seq, id := range []string{"proj-c", "proj-a", "proj-b"} {
		if _, err := d.Execute(context.Background(),
			verified(t, OpProjectRegister, "", map[string]any{"project_id": id}, int64(73+seq)), nil); err != nil {
			t.Fatalf("registering %s: %v", id, err)
		}
	}
	res, _ := d.Execute(context.Background(),
		verified(t, OpProjectRegister, "", map[string]any{"project_id": "proj-a"}, 76), nil)
	var report ProjectRegistration
	_ = json.Unmarshal([]byte(res.Output), &report)
	if strings.Join(report.Registered, ",") != "proj-a,proj-b,proj-c" {
		t.Errorf("registered = %v; sorted order makes two reports comparable", report.Registered)
	}
}

func TestProjectUnregister_OfAnUnknownProjectSucceedsWithoutChange(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	res, err := d.Execute(context.Background(),
		verified(t, OpProjectUnregister, "", map[string]any{"project_id": "never-registered"}, 77), nil)
	if err != nil {
		t.Fatalf("unregistering an unknown project errored: %v", err)
	}
	var report ProjectRegistration
	_ = json.Unmarshal([]byte(res.Output), &report)
	if report.Changed {
		t.Error("unregistering an unknown project reported a change")
	}
}

func TestProjectUnregister_DropsThatProjectsInjectedSecrets(t *testing.T) {
	// Leaving them would keep credentials in memory for a project this agent has been told to stop
	// serving, which is exactly the lifetime FR-45 is careful about.
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	inner := d.(*dispatcher)
	if _, err := d.Execute(context.Background(), verified(t, OpSecretsInject, "approval-r-1", map[string]any{
		"project_id": "proj-d",
		"values":     map[string]string{"TOKEN": syntheticSecret()},
	}, 78), nil); err != nil {
		t.Fatalf("injecting: %v", err)
	}
	if len(inner.secrets.keys("proj-d")) != 1 {
		t.Fatal("the injection did not land, so this test would pass vacuously")
	}
	if _, err := d.Execute(context.Background(),
		verified(t, OpProjectUnregister, "", map[string]any{"project_id": "proj-d"}, 79), nil); err != nil {
		t.Fatalf("unregistering: %v", err)
	}
	if len(inner.secrets.keys("proj-d")) != 0 {
		t.Error("unregistering left the project's injected secrets in memory")
	}
}

func TestProjectRegistry_RefusesAnAbsentProjectID(t *testing.T) {
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for _, op := range []Operation{OpProjectRegister, OpProjectUnregister} {
		if _, err := d.Execute(context.Background(),
			verified(t, op, "", map[string]any{}, 80), nil); err == nil {
			t.Errorf("%q accepted a request with no project_id", op)
		}
	}
}

// ── the forge operations ─────────────────────────────────────────────────────────────────────────

func TestGitBranchCommitPush_DoesTheThreeStepsInOrder(t *testing.T) {
	client := &recordingGit{}
	d := dispatcherWithGit(t, client)
	res, err := d.Execute(context.Background(), verified(t, OpGitBranchCommitPush, "approval-g-1", map[string]any{
		"base_branch":  "main",
		"branch":       "forgeops/add-dockerfile",
		"paths":        []string{"Dockerfile", "k8s/deployment.yaml"},
		"message":      "chore: add deployment artifacts",
		"author_name":  "ForgeOps",
		"author_email": "agent@forgeops.invalid",
	}, 81), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "pushed" {
		t.Fatalf("status %q", res.Status)
	}
	// The order is the substance: a push before a commit pushes nothing, and a commit before a branch
	// commits onto the base.
	if strings.Join(client.calls, ",") != "CreateBranch,CommitPaths,Push" {
		t.Errorf("calls = %v", client.calls)
	}
	if client.createdBase != "main" || client.createdBranch != "forgeops/add-dockerfile" {
		t.Errorf("branch %q from %q", client.createdBranch, client.createdBase)
	}
	if len(client.committed.Paths) != 2 {
		t.Errorf("committed %v", client.committed.Paths)
	}
	var report GitPushReport
	if err := json.Unmarshal([]byte(res.Output), &report); err != nil {
		t.Fatalf("output is not a push report: %v", err)
	}
	if report.CommitSHA != "abc1234" {
		t.Errorf("commit sha %q", report.CommitSHA)
	}
}

func TestGitBranchCommitPush_DefaultsTheBaseToMain(t *testing.T) {
	client := &recordingGit{}
	d := dispatcherWithGit(t, client)
	if _, err := d.Execute(context.Background(), verified(t, OpGitBranchCommitPush, "approval-g-2", map[string]any{
		"branch":  "forgeops/x",
		"paths":   []string{"Dockerfile"},
		"message": "chore: x",
	}, 82), nil); err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if client.createdBase != "main" {
		t.Errorf("base %q, want main", client.createdBase)
	}
}

func TestGitBranchCommitPush_AFailedPushIsAFailureNotAPartialSuccess(t *testing.T) {
	// The commit exists locally and the push did not happen. A backend told "pushed" would look for a
	// branch on the remote that is not there.
	client := &recordingGit{pushErr: errors.New("remote rejected")}
	d := dispatcherWithGit(t, client)
	_, err := d.Execute(context.Background(), verified(t, OpGitBranchCommitPush, "approval-g-3", map[string]any{
		"branch":  "forgeops/x",
		"paths":   []string{"Dockerfile"},
		"message": "chore: x",
	}, 83), nil)
	if err == nil {
		t.Fatal("a rejected push reported success")
	}
	// The refusal names the commit that exists, so an operator can find it.
	if !strings.Contains(err.Error(), "abc1234") {
		t.Errorf("the failure does not name the local commit: %v", err)
	}
}

func TestGitBranchCommitPush_RefusesIncompleteRequests(t *testing.T) {
	cases := map[string]map[string]any{
		"no branch":  {"paths": []string{"Dockerfile"}, "message": "m"},
		"no message": {"branch": "b", "paths": []string{"Dockerfile"}},
		"no paths":   {"branch": "b", "message": "m"},
	}
	for name, args := range cases {
		client := &recordingGit{}
		d := dispatcherWithGit(t, client)
		if _, err := d.Execute(context.Background(),
			verified(t, OpGitBranchCommitPush, "approval-g-4", args, 84), nil); err == nil {
			t.Errorf("%s was accepted", name)
		}
		if len(client.calls) != 0 {
			t.Errorf("%s reached the client before validation: %v", name, client.calls)
		}
	}
}

func TestGitOpenPR_PassesTheRequestThroughAndReportsTheResult(t *testing.T) {
	client := &recordingGit{}
	d := dispatcherWithGit(t, client)
	res, err := d.Execute(context.Background(), verified(t, OpGitOpenPR, "approval-g-5", map[string]any{
		"owner": "acme",
		"repo":  "widgets",
		"title": "Add deployment artifacts",
		"body":  "Generated by ForgeOps.",
		"head":  "forgeops/add-dockerfile",
	}, 85), nil)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if res.Status != "opened" {
		t.Fatalf("status %q", res.Status)
	}
	if client.openedPR.Owner != "acme" || client.openedPR.Head != "forgeops/add-dockerfile" {
		t.Errorf("request = %+v", client.openedPR)
	}
	if !strings.Contains(res.Output, "42") {
		t.Errorf("the result does not carry the PR number: %s", res.Output)
	}
}

func TestGitOpenPR_RefusesIncompleteRequests(t *testing.T) {
	for _, missing := range []string{"owner", "repo", "title", "head"} {
		args := map[string]any{"owner": "acme", "repo": "widgets", "title": "t", "head": "h"}
		delete(args, missing)
		client := &recordingGit{}
		d := dispatcherWithGit(t, client)
		_, err := d.Execute(context.Background(), verified(t, OpGitOpenPR, "approval-g-6", args, 86), nil)
		if err == nil {
			t.Errorf("a request with no %s was accepted", missing)
			continue
		}
		if !strings.Contains(err.Error(), missing) {
			t.Errorf("the refusal for a missing %s does not name it: %v", missing, err)
		}
	}
}

func TestGitOperations_RefuseByNameWithNoClientWired(t *testing.T) {
	// A named refusal rather than a silent success, on the same argument as `ErrNoIndexer`: reporting
	// success for a push that never happened would have the backend record a change set as delivered.
	d, err := New(Deps{Root: t.TempDir()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	for _, op := range []Operation{OpGitBranchCommitPush, OpGitOpenPR} {
		_, execErr := d.Execute(context.Background(), verified(t, op, "approval-g-7", map[string]any{
			"branch": "b", "paths": []string{"x"}, "message": "m",
			"owner": "o", "repo": "r", "title": "t", "head": "h",
		}, 87), nil)
		if !errors.Is(execErr, ErrNoGitClient) {
			t.Errorf("%q with no client gave %v", op, execErr)
		}
	}
}

func TestGitOperations_AreMutatingAndNeedApproval(t *testing.T) {
	for _, op := range []Operation{OpGitBranchCommitPush, OpGitOpenPR} {
		row := handlerTable[op]
		if !row.mutating || !row.requiresApproval {
			t.Errorf("%q is mutating=%v requiresApproval=%v; both must be true",
				op, row.mutating, row.requiresApproval)
		}
	}
}
