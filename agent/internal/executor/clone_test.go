// SPDX-License-Identifier: Apache-2.0
package executor

// `repository.clone`'s tests.
//
// THE CREDENTIAL ASSERTION IS THE POINT OF THIS FILE, and it is made against a repository that REQUIRES
// authentication — a private one, served by a local HTTP server that refuses an unauthenticated fetch
// with 401. Testing the property against a public repository would prove nothing: a clone that never
// needed a credential cannot leave one behind. The server also records the credential it was handed, so
// the test can prove the fetch really was authenticated rather than passing because nothing was sent.
//
// `git clone https://token@host/...` is the spelling this operation exists to avoid, so one test
// deliberately writes a credential into `.git/config` by hand and requires `assertNoCredentialOnDisk`
// to find it. Without that, a broken assertion would pass every other test in this file.

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The synthetic credential. Self-labelling, and not shaped like a real GitHub token: the repository's
// own scanners refuse a credential-shaped literal in a test source, and the shape is not what is under
// test — the presence of these bytes on disk is.
const testCloneToken = "clone-test-credential-value-only-used-here"

// newPrivateRepository serves one commit over HTTP and refuses an unauthenticated request.
//
// `git http-backend` is used rather than go-git's server because the property under test is what lands
// in the working tree after a REAL fetch over HTTP with basic auth, and the dumb-protocol shortcuts
// would not exercise the same path. Skipped when `git` is absent, with the reason stated: a silent skip
// is how a credential assertion stops being made.
func newPrivateRepository(t *testing.T) (url string, seenAuth *[]string) {
	t.Helper()
	gitBinary, err := exec.LookPath("git")
	if err != nil {
		t.Skip("platform-only: `git` is not on PATH, and the private-repository fetch needs git-http-backend")
	}

	root := t.TempDir()
	repo := filepath.Join(root, "private.git")
	run := func(args ...string) {
		t.Helper()
		cmd := exec.Command(gitBinary, args...) //nolint:gosec // fixed binary, fixed arguments
		cmd.Dir = root
		if out, runErr := cmd.CombinedOutput(); runErr != nil {
			t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), runErr, out)
		}
	}
	work := filepath.Join(root, "work")
	if mkErr := os.MkdirAll(work, 0o750); mkErr != nil {
		t.Fatalf("mkdir: %v", mkErr)
	}
	if writeErr := os.WriteFile(filepath.Join(work, "README.md"), []byte("# private\n"), 0o600); writeErr != nil {
		t.Fatalf("write: %v", writeErr)
	}
	run("init", "--quiet", "--initial-branch=main", "work")
	inWork := func(args ...string) {
		t.Helper()
		cmd := exec.Command(gitBinary, args...) //nolint:gosec // fixed binary, fixed arguments
		cmd.Dir = work
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=clone-test", "GIT_AUTHOR_EMAIL=clone@example.invalid",
			"GIT_COMMITTER_NAME=clone-test", "GIT_COMMITTER_EMAIL=clone@example.invalid")
		if out, runErr := cmd.CombinedOutput(); runErr != nil {
			t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), runErr, out)
		}
	}
	inWork("add", "README.md")
	inWork("commit", "--quiet", "-m", "initial")
	run("clone", "--quiet", "--bare", "work", "private.git")

	recorded := make([]string, 0, 4)
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		if !ok || password != testCloneToken {
			// A REAL 401. Without it a clone that sent no credential would succeed and the assertion
			// below would be vacuous.
			w.Header().Set("WWW-Authenticate", `Basic realm="private"`)
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		recorded = append(recorded, user)
		cgi := exec.Command(gitBinary, "http-backend") //nolint:gosec // fixed binary
		serveCGI(w, r, cgi, repo)
	})
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server.URL + "/private.git", &recorded
}

// serveCGI runs `git http-backend` for one request. Small enough to keep here rather than depend on a
// CGI package: what matters is that the transport is the real smart protocol.
func serveCGI(w http.ResponseWriter, r *http.Request, cmd *exec.Cmd, repo string) {
	cmd.Env = append(os.Environ(),
		"GIT_PROJECT_ROOT="+filepath.Dir(repo),
		"GIT_HTTP_EXPORT_ALL=1",
		"REQUEST_METHOD="+r.Method,
		"PATH_INFO="+r.URL.Path,
		"QUERY_STRING="+r.URL.RawQuery,
		"CONTENT_TYPE="+r.Header.Get("Content-Type"),
		"CONTENT_LENGTH="+r.Header.Get("Content-Length"),
		"GIT_PROTOCOL="+r.Header.Get("Git-Protocol"),
		"REMOTE_USER=clone-test",
		"SERVER_PROTOCOL="+r.Proto,
		"GATEWAY_INTERFACE=CGI/1.1",
	)
	cmd.Stdin = r.Body
	out, err := cmd.Output()
	if err != nil {
		http.Error(w, "backend failed", http.StatusInternalServerError)
		return
	}
	// Split the CGI headers from the body and forward both.
	if split := strings.Index(string(out), "\r\n\r\n"); split >= 0 {
		for _, line := range strings.Split(string(out[:split]), "\r\n") {
			if colon := strings.Index(line, ":"); colon > 0 {
				w.Header().Set(strings.TrimSpace(line[:colon]), strings.TrimSpace(line[colon+1:]))
			}
		}
		_, _ = w.Write(out[split+4:])
		return
	}
	_, _ = w.Write(out)
}

// cloneSeq keeps every test envelope's sequence number distinct, because the replay guard is real.
var cloneSeq int64 = 9000

// runClone drives the operation through `Execute` rather than calling the handler directly.
//
// Through the dispatcher on purpose: `Execute` is where the approval requirement, the timeout and the
// catalogue lookup live, so a clone that reached its body without an approval would pass a test that
// called the handler and fail in production.
func runClone(t *testing.T, root string, args cloneArgs) (Result, error) {
	t.Helper()
	d := newDispatcher(t, root)
	cloneSeq++
	v := verified(t, OpRepositoryClone, "approval-for-the-clone-test", args, cloneSeq)
	return d.Execute(context.Background(), v, SinkFunc(func(int, string, string) {}))
}

func TestClone_RefusesWithoutAnApproval(t *testing.T) {
	// §7.7's third column, exercised rather than assumed: a clone with no approval id must not run.
	d := newDispatcher(t, t.TempDir())
	cloneSeq++
	v := verified(t, OpRepositoryClone, "", cloneArgs{
		ProjectID: "11111111-2222-3333-4444-555555555555",
		CloneURL:  "https://example.invalid/o/r.git",
	}, cloneSeq)

	_, err := d.Execute(context.Background(), v, SinkFunc(func(int, string, string) {}))

	if err == nil {
		t.Fatal("a clone without an approval was executed")
	}
}

func TestClone_FetchesAPrivateRepositoryAndLeavesNoCredentialOnDisk(t *testing.T) {
	url, seenAuth := newPrivateRepository(t)
	root := t.TempDir()

	result, err := runClone(t, root, cloneArgs{
		ProjectID:    "11111111-2222-3333-4444-555555555555",
		CloneURL:     url,
		RepoFullName: "octo-org/private",
		Token:        testCloneToken,
	})
	if err != nil {
		t.Fatalf("clone: %v", err)
	}

	var report CloneReport
	if err := json.Unmarshal([]byte(result.Output), &report); err != nil {
		t.Fatalf("report: %v", err)
	}
	if len(*seenAuth) == 0 {
		t.Fatal("the server saw no authenticated request; the assertion below would be vacuous")
	}
	if report.Path != filepath.Join(root, "private") {
		t.Errorf("cloned to %q, want %q", report.Path, filepath.Join(root, "private"))
	}
	if !report.Shallow || report.Depth != CloneDepth {
		t.Errorf("depth = %d shallow = %v, want %d and true", report.Depth, report.Shallow, CloneDepth)
	}
	if report.FileCount == 0 || report.SizeBytes == 0 {
		t.Errorf("report claims an empty clone: %+v", report)
	}
	if !report.CredentialAbsent {
		t.Error("the report must state that the credential assertion ran")
	}
	if _, statErr := os.Stat(filepath.Join(report.Path, "README.md")); statErr != nil {
		t.Errorf("the working tree is missing its file: %v", statErr)
	}

	// THE ASSERTION. Everything under the clone, including `.git/config`, must be free of the token.
	if err := assertNoCredentialOnDisk(report.Path, testCloneToken); err != nil {
		t.Errorf("a credential reached the working tree: %v", err)
	}
	config, err := os.ReadFile(filepath.Join(report.Path, ".git", "config"))
	if err != nil {
		t.Fatalf("read .git/config: %v", err)
	}
	if strings.Contains(string(config), testCloneToken) {
		t.Error(".git/config contains the credential, which is the exact failure this operation avoids")
	}
	// And the remote URL is the one that was asked for, with no userinfo spliced into it.
	if !strings.Contains(string(config), url) || strings.Contains(string(config), "@"+strings.TrimPrefix(url, "http://")) {
		t.Errorf(".git/config's remote is not the credential-free URL:\n%s", config)
	}
	// The error message must not carry it either, so a failing clone cannot log one.
	if sanitised := sanitise(fmt.Errorf("auth failed for %s", testCloneToken), testCloneToken); strings.Contains(sanitised.Error(), testCloneToken) {
		t.Error("sanitise left the credential in the message")
	}
}

func TestClone_TheCredentialAssertionActuallyFinds(t *testing.T) {
	// The negative control for the assertion itself. Without this, a broken `assertNoCredentialOnDisk`
	// would make every other test in this file pass for the wrong reason.
	root := t.TempDir()
	tree := filepath.Join(root, "repo", ".git")
	if err := os.MkdirAll(tree, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	config := filepath.Join(tree, "config")
	body := "[remote \"origin\"]\n\turl = https://" + testCloneToken + "@example.invalid/o/r.git\n"
	if err := os.WriteFile(config, []byte(body), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	err := assertNoCredentialOnDisk(filepath.Join(root, "repo"), testCloneToken)

	if err == nil {
		t.Fatal("a credential in .git/config was not found")
	}
	if !strings.Contains(err.Error(), filepath.Join(".git", "config")) {
		t.Errorf("the error must name the offending path, got %v", err)
	}
	if strings.Contains(err.Error(), testCloneToken) {
		t.Error("the error message carries the credential it is complaining about")
	}
}

func TestClone_RefusesTheNamesThatEscapeOrCollide(t *testing.T) {
	for _, name := range []string{
		"", " ", "leading-space-check ", ".", "..", "../escape", "a/b", `a\b`,
		"C:evil", "aux", "AUX", "nul.txt", "com1", "trailing.",
	} {
		if err := validateCloneName(name); err == nil {
			t.Errorf("%q was accepted as a directory name", name)
		}
	}
	for _, name := range []string{"deploy-me", "notes", "a.b.c", "auxiliary", "com10", "ForgeOps"} {
		if err := validateCloneName(name); err != nil {
			t.Errorf("%q was refused: %v", name, err)
		}
	}
}

func TestClone_RefusesAParentOutsideTheWorkspaceRoot(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()

	_, err := resolveCloneTarget(root, outside, "repo")

	if err == nil {
		t.Fatal("a parent outside the root was accepted")
	}
	if !strings.Contains(err.Error(), "outside") || !strings.Contains(err.Error(), "AGENT_WORKSPACE_ROOT") {
		t.Errorf("the refusal must name the root and how to change it, got %v", err)
	}
}

func TestClone_AcceptsTheRootAndDirectoriesBeneathIt(t *testing.T) {
	root := t.TempDir()
	nested := filepath.Join(root, "workspaces", "team")

	atRoot, err := resolveCloneTarget(root, "", "repo")
	if err != nil {
		t.Fatalf("the root itself was refused: %v", err)
	}
	if atRoot != filepath.Join(root, "repo") {
		t.Errorf("resolved to %q", atRoot)
	}

	beneath, err := resolveCloneTarget(root, nested, "repo")
	if err != nil {
		t.Fatalf("a directory beneath the root was refused: %v", err)
	}
	if beneath != filepath.Join(nested, "repo") {
		t.Errorf("resolved to %q", beneath)
	}
}

func TestClone_RefusesANonEmptyTargetByName(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "occupied")
	if err := os.MkdirAll(target, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(target, "keep.txt"), []byte("mine\n"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	err := ensureEmptyTarget(target)

	if err == nil {
		t.Fatal("a non-empty target was accepted")
	}
	if !strings.Contains(err.Error(), target) {
		t.Errorf("the refusal must name the path, got %v", err)
	}
	// And the file is still there: a refusal is a refusal, not a delete.
	if _, statErr := os.Stat(filepath.Join(target, "keep.txt")); statErr != nil {
		t.Errorf("the refusal removed the user's file: %v", statErr)
	}
}

func TestClone_AnEmptyExistingDirectoryIsFine(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "empty")
	if err := os.MkdirAll(target, 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	if err := ensureEmptyTarget(target); err != nil {
		t.Errorf("an empty directory was refused: %v", err)
	}
}

func TestClone_RefusesACaseOnlyCollision(t *testing.T) {
	// Refused on EVERY platform, including case-sensitive ones: a project must mean the same thing on
	// every machine, and a case-sensitive agent creating `Repo` beside `repo` would produce a checkout
	// a Windows agent cannot reproduce.
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "Repo"), 0o750); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	err := ensureEmptyTarget(filepath.Join(root, "repo"))

	if err == nil {
		t.Fatalf("a case-only collision was accepted (runtime %s)", runtime.GOOS)
	}
	if !strings.Contains(err.Error(), "case") {
		t.Errorf("the refusal must say what collided, got %v", err)
	}
}

func TestClone_RefusesAURLCarryingUserinfo(t *testing.T) {
	root := t.TempDir()

	_, err := runClone(t, root, cloneArgs{
		ProjectID:    "11111111-2222-3333-4444-555555555555",
		CloneURL:     "https://" + testCloneToken + "@example.invalid/o/r.git",
		RepoFullName: "o/r",
	})

	if err == nil {
		t.Fatal("a URL with userinfo was accepted")
	}
	if !strings.Contains(err.Error(), "userinfo") {
		t.Errorf("the refusal must name the reason, got %v", err)
	}
}

func TestClone_RefusesWithoutAProjectOrAURL(t *testing.T) {
	root := t.TempDir()

	if _, err := runClone(t, root, cloneArgs{CloneURL: "https://example.invalid/o/r.git"}); err == nil {
		t.Error("a clone with no project_id was accepted")
	}
	if _, err := runClone(t, root, cloneArgs{ProjectID: "p"}); err == nil {
		t.Error("a clone with no clone_url was accepted")
	}
}

func TestClone_MeasuresWhatItWroteAndStopsPastTheLimit(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "a.bin"), make([]byte, 1024), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	size, files, over, err := measureTree(root, 0)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if size != 1024 || files != 1 || over {
		t.Errorf("measured %d bytes in %d file(s), over=%v", size, files, over)
	}

	_, _, over, err = measureTree(root, 16)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if !over {
		t.Error("a tree past the limit was not reported as over")
	}
}

func TestClone_FailureLeavesNothingBehind(t *testing.T) {
	// An unreachable URL. The partial directory must not survive: a project pointing at a directory a
	// scan would index as an empty repository is the failure mode this removal exists to avoid.
	root := t.TempDir()

	_, err := runClone(t, root, cloneArgs{
		ProjectID:    "11111111-2222-3333-4444-555555555555",
		CloneURL:     "http://127.0.0.1:1/nope.git",
		RepoFullName: "o/nope",
	})

	if err == nil {
		t.Fatal("a clone from an unreachable host succeeded")
	}
	if _, statErr := os.Stat(filepath.Join(root, "nope")); !os.IsNotExist(statErr) {
		t.Errorf("a partial checkout survived the failure: %v", statErr)
	}
}

func TestClone_DefaultsTheDirectoryNameToTheRepositoryName(t *testing.T) {
	if got := defaultDirectoryName("octo-org/deploy-me"); got != "deploy-me" {
		t.Errorf("defaultDirectoryName = %q", got)
	}
	if got := defaultDirectoryName("standalone"); got != "standalone" {
		t.Errorf("defaultDirectoryName = %q", got)
	}
}
