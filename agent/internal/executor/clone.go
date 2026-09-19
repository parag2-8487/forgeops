// SPDX-License-Identifier: Apache-2.0
package executor

// `repository.clone`: put a repository on the operator's machine, once, under the workspace root.
//
// WHY THIS IS AN AGENT OPERATION AND NOT A BACKEND SIDE EFFECT. The backend cannot write to the user's
// filesystem — that is the whole Tier 3 boundary — so a clone is a named, whitelisted, signed,
// policy-checked and audited operation like every other write. There is deliberately no shell: this
// calls a Git library in-process, so there is no command line for a credential to appear on and nothing
// for a `ps` listing to show another user.
//
// THE CREDENTIAL NEVER REACHES THE WORKING TREE, and that is a property of HOW the clone is done rather
// than of a cleanup step afterwards. `git clone https://token@host/...` writes the credential into
// `.git/config`, where it stays for the life of the checkout and travels with any backup of it. Here the
// URL carries no credential and the token is supplied as per-operation HTTP basic auth held in memory
// only — go-git does not persist it — and the result is asserted: `assertNoCredentialOnDisk` walks the
// cloned tree, including `.git`, and fails the operation if the token appears anywhere. A refusal after
// the fact is not the defence; the defence is that there is nothing to find. The walk is there because
// "nothing writes it" is a claim, and this file is where it gets checked.
//
// WHAT IT REFUSES, all before a byte is fetched:
//   - a parent directory outside the agent's configured workspace root, naming the root;
//   - a directory name that is not a single path segment: traversal, a separator, an absolute path, a
//     drive letter, a reserved Windows device name, a trailing dot or space;
//   - a name that differs from an existing entry only by case, which on a case-insensitive filesystem
//     would silently clone into the other one;
//   - an existing target that is not empty, naming the path.
//
// AND IT IS BOUNDED: shallow by default at depth 1 with the depth stated in the result, a context
// timeout from the dispatch table, and a size ceiling checked after the fetch — over which the partial
// checkout is removed rather than left for a scan to index half a repository.
//
// NOTHING ON THE BACKEND MINTS THIS COMMAND YET, and that is stated here rather than left for a reader
// to discover. The operation is implemented, dispatchable, approval-gated and tested — including against
// a repository that requires authentication — but the backend transit that would mint it does not exist:
// §2.2.1 confines `send_command` to `governance/`, so a clone trigger is a governance decision and needs
// a chokepoint transit of its own (policy, approval gate, blast radius, audit, rollback handle), not a
// route. `scan.full` and `scan.incremental` are in the same position for the same reason, and the agent's
// `scan` CLI verb documents it. What the transit needs, so the next reader does not have to rediscover
// it: a `change_sets` row of a new origin whose single item is the clone rather than a file write, a
// blast-radius score for "one directory created", and a rollback handle that records the directory to
// remove. Until it exists, the only way to reach this operation is a signed envelope minted by
// `governance/`, which is the intended and only path.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	gogit "github.com/go-git/go-git/v5"
	"github.com/go-git/go-git/v5/plumbing"
	githttp "github.com/go-git/go-git/v5/plumbing/transport/http"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
)

// CloneDepth is the shallow depth every clone uses unless the envelope asks for the full history.
//
// Shallow BY DEFAULT rather than on request: the product's purpose is to read the current state of a
// repository and write DevOps configuration into it, and none of that needs history. A full clone of a
// large repository is minutes of network and gigabytes of disk for information nothing reads.
const CloneDepth = 1

// MaxCloneBytes bounds what a clone may put on the machine. 2 GiB is generous for a source repository
// and small enough that a mistake — a repository of build artifacts, a misconfigured LFS — is caught
// rather than filling a disk.
const MaxCloneBytes int64 = 2 << 30

// The username GitHub requires beside an installation or user-to-server token in basic auth. Any
// non-empty value works and GitHub documents this one; it is not a credential.
const cloneAuthUsername = "x-access-token"

// Errors this operation returns. Each is a refusal a human can act on, and each names the offending
// value rather than saying "invalid".
var (
	// ErrCloneOutsideRoot is a parent directory that is not the workspace root or beneath it.
	ErrCloneOutsideRoot = errors.New("executor: the clone parent is outside the agent's workspace root")
	// ErrCloneBadName is a directory name that is not a single, safe path segment.
	ErrCloneBadName = errors.New("executor: the clone directory name is not a single safe path segment")
	// ErrCloneTargetExists is a target directory that exists and is not empty.
	ErrCloneTargetExists = errors.New("executor: the clone target exists and is not empty")
	// ErrCloneTooLarge is a repository past MaxCloneBytes.
	ErrCloneTooLarge = errors.New("executor: the cloned repository exceeds the size limit")
	// ErrCloneCredentialOnDisk is the assertion failing: a token was found under the clone.
	ErrCloneCredentialOnDisk = errors.New("executor: a credential was found inside the cloned tree")
)

// cloneArgs is `repository.clone`'s argument object.
//
// `Token` IS IN THE ENVELOPE AND NOWHERE ELSE. It arrives over mutual TLS inside an HMAC-signed
// envelope, is used for one fetch, and is never written down: it is absent from the result, from the
// progress messages, from every error in this file, and — asserted — from the tree it produced. The
// backend's audit row records the repository and the path, never this field.
type cloneArgs struct {
	ProjectID string `json:"project_id"`
	// The full HTTPS clone URL, with no credential in it. A URL carrying one is refused rather than
	// accepted and cleaned, because accepting it would mean the credential had already been written
	// into whatever logged the command.
	CloneURL string `json:"clone_url"`
	// `owner/name`, for the progress messages and the result. Not used to build a path.
	RepoFullName string `json:"repo_full_name"`
	// Where to put it. Absolute, and required to be the workspace root or beneath it.
	ParentDirectory string `json:"parent_directory"`
	// One path segment. Defaults to the repository name when empty.
	DirectoryName string `json:"directory_name"`
	// Optional. Defaults to the repository's default branch.
	Branch string `json:"branch"`
	// Absent or false means shallow at CloneDepth. Present so a caller that genuinely needs history
	// can ask, and so the result can state which it did.
	FullHistory bool `json:"full_history"`
	// The short-lived credential, for a private repository. Empty for a public one.
	Token string `json:"token"`
}

// CloneReport is what the operation returns, and what the backend records as the project's path.
//
// NO TOKEN FIELD, and the absence is the point: a result type with a credential field is a credential
// in every log that serialises a result.
type CloneReport struct {
	ProjectID  string `json:"project_id"`
	Repository string `json:"repository"`
	// The absolute path the agent actually created. The backend writes this onto the project row, so
	// the project's path is what exists on disk rather than what somebody typed.
	Path   string `json:"path"`
	Branch string `json:"branch"`
	Depth  int    `json:"depth"`
	// Bytes on disk after the clone, so an operator can see what landed without measuring it.
	SizeBytes int64 `json:"size_bytes"`
	FileCount int   `json:"file_count"`
	Shallow   bool  `json:"shallow"`
	// Reported so a reader of the result knows the assertion ran rather than assuming it.
	CredentialAbsent bool `json:"credential_absent"`
}

// reservedWindowsNames are refused on every platform, not only Windows.
//
// A repository named `aux` is legal on Linux and unopenable on Windows, and a project created on one
// machine is expected to work on another. Refusing everywhere is the answer that does not depend on
// where the agent happens to run; refusing only on Windows would let a Linux agent create a directory
// that a Windows agent later cannot touch, and the failure would surface as a scan finding nothing.
var reservedWindowsNames = map[string]struct{}{
	"con": {}, "prn": {}, "aux": {}, "nul": {},
	"com1": {}, "com2": {}, "com3": {}, "com4": {}, "com5": {},
	"com6": {}, "com7": {}, "com8": {}, "com9": {},
	"lpt1": {}, "lpt2": {}, "lpt3": {}, "lpt4": {}, "lpt5": {},
	"lpt6": {}, "lpt7": {}, "lpt8": {}, "lpt9": {},
}

// validateCloneName checks the directory name in isolation, before any filesystem is touched.
//
// Exported for its test rather than tested through a clone: these are the rules a reviewer most needs
// to be able to read and a test most needs to be able to enumerate, and driving each through a real
// fetch would make the enumeration slow enough that somebody would trim it.
func validateCloneName(name string) error {
	trimmed := strings.TrimSpace(name)
	switch {
	case trimmed == "":
		return fmt.Errorf("%w: the name is empty", ErrCloneBadName)
	case trimmed != name:
		// Refused rather than trimmed. A name with a leading or trailing space is a name whose
		// on-disk spelling differs from what the user believes it is, and Windows silently strips
		// trailing ones — so the directory the project points at would not be the one created.
		return fmt.Errorf("%w: %q has leading or trailing whitespace", ErrCloneBadName, name)
	case name == "." || name == "..":
		return fmt.Errorf("%w: %q is a traversal", ErrCloneBadName, name)
	case strings.ContainsAny(name, `/\`):
		return fmt.Errorf("%w: %q contains a path separator; one segment only", ErrCloneBadName, name)
	case strings.Contains(name, ".."):
		return fmt.Errorf("%w: %q contains a traversal sequence", ErrCloneBadName, name)
	case strings.ContainsRune(name, ':'):
		// A drive letter or an NTFS alternate data stream. Both are ways to write somewhere else.
		return fmt.Errorf("%w: %q contains a colon", ErrCloneBadName, name)
	case strings.ContainsRune(name, 0):
		return fmt.Errorf("%w: the name contains a NUL byte", ErrCloneBadName)
	case strings.HasSuffix(name, "."):
		return fmt.Errorf("%w: %q ends in a dot, which Windows strips", ErrCloneBadName, name)
	case filepath.IsAbs(name):
		return fmt.Errorf("%w: %q is absolute", ErrCloneBadName, name)
	}
	// The reserved check is on the stem, because `aux.txt` is as unopenable as `aux` on Windows.
	stem := strings.ToLower(name)
	if dot := strings.IndexByte(stem, '.'); dot > 0 {
		stem = stem[:dot]
	}
	if _, reserved := reservedWindowsNames[stem]; reserved {
		return fmt.Errorf("%w: %q is a reserved device name on Windows", ErrCloneBadName, name)
	}
	return nil
}

// resolveCloneTarget turns the arguments into one absolute path, or refuses.
//
// The parent must be the workspace root or beneath it. That is the same confinement every write in this
// agent has, and it is what makes the typed parent directory safe to accept at all: a signed envelope
// that could nominate any absolute path would let the backend — or anything that could get a command
// signed — write outside the directory the operator handed to the agent.
func resolveCloneTarget(root, parent, name string) (string, error) {
	if err := validateCloneName(name); err != nil {
		return "", err
	}
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return "", fmt.Errorf("executor: unusable workspace root %q: %w", root, err)
	}
	// An empty parent means the root itself, which is the common case: the operator chose the location
	// when they started the agent.
	candidate := strings.TrimSpace(parent)
	if candidate == "" {
		candidate = absRoot
	}
	absParent, err := filepath.Abs(candidate)
	if err != nil {
		return "", fmt.Errorf("executor: unusable parent directory %q: %w", parent, err)
	}
	// EvalSymlinks on the root, not on the parent: the parent may not exist yet, and the root always
	// does. Without this a symlinked root (a bind mount, a Docker Desktop share) compares unequal to
	// the resolved parent and every clone is refused for a reason that is not true.
	if resolved, resolveErr := filepath.EvalSymlinks(absRoot); resolveErr == nil {
		absRoot = resolved
	}
	if resolvedParent, resolveErr := filepath.EvalSymlinks(absParent); resolveErr == nil {
		absParent = resolvedParent
	}
	relative, err := filepath.Rel(absRoot, absParent)
	if err != nil {
		return "", fmt.Errorf("%w: %q is not comparable to %q", ErrCloneOutsideRoot, absParent, absRoot)
	}
	if relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf(
			"%w: %q is outside %q. The agent writes only under the workspace root it was started with; "+
				"restart it with AGENT_WORKSPACE_ROOT set to the directory you want, or choose a "+
				"directory inside it", ErrCloneOutsideRoot, absParent, absRoot)
	}
	return filepath.Join(absParent, name), nil
}

// ensureEmptyTarget refuses a non-empty directory by name, and refuses a case-only collision.
func ensureEmptyTarget(target string) error {
	parent := filepath.Dir(target)
	wanted := filepath.Base(target)
	entries, err := os.ReadDir(parent)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		// The parent does not exist yet, so nothing can collide and there is nothing to read.
		return nil
	case err != nil:
		return fmt.Errorf("executor: cannot read %q: %w", parent, err)
	}
	for _, entry := range entries {
		if entry.Name() == wanted {
			continue
		}
		if strings.EqualFold(entry.Name(), wanted) {
			// A CASE-ONLY COLLISION. On Windows and on macOS's default filesystem the clone would go
			// into the existing directory under its existing spelling, so the project would point at a
			// path that does not exist as written. Refused everywhere, for the reason the reserved
			// names are: a project must mean the same thing on every machine.
			return fmt.Errorf(
				"%w: %q already exists in %q and differs from %q only by case",
				ErrCloneTargetExists, entry.Name(), parent, wanted)
		}
	}
	contents, err := os.ReadDir(target)
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return nil
	case err != nil:
		return fmt.Errorf("executor: cannot read %q: %w", target, err)
	case len(contents) > 0:
		return fmt.Errorf(
			"%w: %s already contains %d entry(ies). Nothing was changed; choose another directory name "+
				"or remove that directory first", ErrCloneTargetExists, target, len(contents))
	}
	return nil
}

// measureTree returns the byte size and file count under root, and stops early past the limit.
func measureTree(root string, limit int64) (int64, int, bool, error) {
	var total int64
	var files int
	over := false
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		info, statErr := d.Info()
		if statErr != nil {
			// A file that vanished mid-walk is not a measurement failure; it is a file that is not
			// there to measure.
			if errors.Is(statErr, fs.ErrNotExist) {
				return nil
			}
			return statErr
		}
		files++
		total += info.Size()
		if limit > 0 && total > limit {
			over = true
			return filepath.SkipAll
		}
		return nil
	})
	if err != nil {
		return total, files, over, fmt.Errorf("executor: cannot measure %q: %w", root, err)
	}
	return total, files, over, nil
}

// assertNoCredentialOnDisk is the check that makes the "no token in the working tree" claim testable.
//
// It walks everything under the clone INCLUDING `.git`, because `.git/config` is exactly where the
// naive spelling of this operation puts a credential. Text-only comparison on the raw bytes: a token is
// ASCII and a search for it in a binary object file costs nothing extra.
//
// Skipped for an empty token, which is the public-repository case — searching for the empty string would
// match every file and the operation would refuse every public clone.
func assertNoCredentialOnDisk(root, token string) error {
	if strings.TrimSpace(token) == "" {
		return nil
	}
	needle := []byte(token)
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		// A credential is short; a file smaller than it cannot contain it. Cheap, and it skips the
		// thousands of tiny loose objects a clone produces.
		info, statErr := d.Info()
		if statErr != nil || info.Size() < int64(len(needle)) {
			return nil //nolint:nilerr // an unmeasurable file is not evidence of a credential
		}
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			return nil //nolint:nilerr // an unreadable file cannot be asserted about; the walk continues
		}
		if bytes.Contains(data, needle) {
			// The PATH is named and the token is not. An error message carrying the credential it is
			// complaining about would put it in the log that the message exists to prevent.
			relative, relErr := filepath.Rel(root, path)
			if relErr != nil {
				relative = path
			}
			return fmt.Errorf("%w: %s", ErrCloneCredentialOnDisk, relative)
		}
		return nil
	})
}

// repositoryClone is the handler.
func repositoryClone(ctx context.Context, d *dispatcher, v *envelope.Verified, sink ProgressSink) (Result, error) {
	var args cloneArgs
	if err := json.Unmarshal(v.Args(), &args); err != nil {
		return Result{}, fmt.Errorf("executor: undecodable clone arguments: %w", err)
	}
	if strings.TrimSpace(args.ProjectID) == "" {
		return Result{}, errors.New("executor: a clone needs a project_id")
	}
	url := strings.TrimSpace(args.CloneURL)
	if url == "" {
		return Result{}, errors.New("executor: a clone needs a clone_url")
	}
	if strings.Contains(url, "@") && strings.HasPrefix(url, "http") {
		// A URL with userinfo in it. Refused rather than sanitised: if it arrived here it has already
		// been through the backend's logging and the envelope's audit trail.
		return Result{}, errors.New(
			"executor: the clone URL carries userinfo; credentials travel in the envelope's token field, " +
				"never in the URL")
	}
	name := strings.TrimSpace(args.DirectoryName)
	if name == "" {
		name = defaultDirectoryName(args.RepoFullName)
	}
	target, err := resolveCloneTarget(d.root, args.ParentDirectory, name)
	if err != nil {
		return Result{}, err
	}
	if err := ensureEmptyTarget(target); err != nil {
		return Result{}, err
	}

	depth := CloneDepth
	if args.FullHistory {
		depth = 0
	}
	options := &gogit.CloneOptions{
		URL:   url,
		Depth: depth,
		// Tags are history. A shallow clone that still fetched every tag would defeat the point.
		Tags:              gogit.NoTags,
		SingleBranch:      !args.FullHistory,
		RecurseSubmodules: gogit.NoRecurseSubmodules,
	}
	if branch := strings.TrimSpace(args.Branch); branch != "" {
		options.ReferenceName = branchReference(branch)
	}
	if token := strings.TrimSpace(args.Token); token != "" {
		// PER-OPERATION AUTH, held in memory. This is the whole reason the credential never reaches
		// `.git/config`: the URL carries none, and go-git does not persist what it was handed here.
		options.Auth = &githttp.BasicAuth{Username: cloneAuthUsername, Password: token}
	}

	sink.Progress(5, "repository.clone", fmt.Sprintf("cloning %s into %s", args.RepoFullName, target))
	started := time.Now()
	if _, err := gogit.PlainCloneContext(ctx, target, false, options); err != nil {
		// THE PARTIAL CHECKOUT IS REMOVED. A failed clone that left a directory behind would leave the
		// project pointing at something a scan would index as a repository with no files in it.
		_ = os.RemoveAll(target)
		return Result{}, fmt.Errorf("executor: cloning %s failed: %w", args.RepoFullName, sanitise(err, args.Token))
	}

	size, files, over, err := measureTree(target, MaxCloneBytes)
	if err != nil {
		_ = os.RemoveAll(target)
		return Result{}, err
	}
	if over {
		_ = os.RemoveAll(target)
		return Result{}, fmt.Errorf(
			"%w: %s exceeds %d bytes; nothing was left on disk", ErrCloneTooLarge, args.RepoFullName, MaxCloneBytes)
	}
	if err := assertNoCredentialOnDisk(target, args.Token); err != nil {
		// Removed, because a tree with a credential in it is worse than no tree: it is a credential on
		// the operator's disk that nobody knows about.
		_ = os.RemoveAll(target)
		return Result{}, err
	}

	report := CloneReport{
		ProjectID:        args.ProjectID,
		Repository:       args.RepoFullName,
		Path:             target,
		Branch:           strings.TrimSpace(args.Branch),
		Depth:            depth,
		SizeBytes:        size,
		FileCount:        files,
		Shallow:          depth > 0,
		CredentialAbsent: true,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable clone report: %w", err)
	}
	sink.Progress(100, "repository.clone", fmt.Sprintf(
		"cloned %s into %s (%d file(s), %d byte(s), %s)",
		args.RepoFullName, target, files, size, time.Since(started).Round(time.Millisecond)))
	return Result{Status: "cloned", Output: string(encoded)}, nil
}

// defaultDirectoryName is the repository's own name, which is what a person expects.
func defaultDirectoryName(fullName string) string {
	if slash := strings.LastIndexByte(fullName, '/'); slash >= 0 && slash+1 < len(fullName) {
		return fullName[slash+1:]
	}
	return strings.TrimSpace(fullName)
}

// branchReference spells a branch as the reference go-git wants, accepting either form.
func branchReference(branch string) plumbing.ReferenceName {
	if strings.HasPrefix(branch, "refs/") {
		return plumbing.ReferenceName(branch)
	}
	return plumbing.ReferenceName("refs/heads/" + branch)
}

// sanitise removes a credential from an error before it is returned.
//
// BELT TO THE BRACES of never putting the token in a URL: a transport error can quote the request it
// failed on, and `git`'s own messages sometimes echo the credential helper's output. The token is
// replaced rather than the message dropped, because the rest of the message is the diagnosis.
func sanitise(err error, token string) error {
	if err == nil || strings.TrimSpace(token) == "" {
		return err
	}
	message := err.Error()
	if !strings.Contains(message, token) {
		return err
	}
	return errors.New(strings.ReplaceAll(message, token, "<credential withheld>"))
}
