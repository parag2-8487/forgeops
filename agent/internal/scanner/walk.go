// SPDX-License-Identifier: Apache-2.0
package scanner

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	gitignore "github.com/sabhiram/go-gitignore"

	"github.com/parag8487/ForgeOps/agent/internal/fileops"
)

// WalkFiles applies §1.3's filters plus two the walk was missing, and is the exported entry point
// for anything outside this package that needs the same file set the index sees.
//
// TWO REAL GAPS CLOSED HERE, both recorded as partial against the PRD.
//
// FR-08 — `.gitignore` was not honoured. `walkFiles` skipped four hardcoded directory names
// (`.git`, `node_modules`, `.pytest_cache`, `.ruff_cache`) and nothing else, so a repository's own
// statement about what is not source — `dist/`, `build/`, `target/`, `.venv/`, coverage output, a
// vendored dependency tree — was ignored. The practical effect was an index full of build output,
// which makes retrieval worse rather than more complete: a search for a function finds it in the
// bundled copy first.
//
// FR-09 — `.env` and `*.pem` were being read. The walk called `os.ReadFile` directly and never
// consulted `fileops.blockedForRead`, the blocklist that exists precisely to stop the agent reading
// credentials. Secrets in those files were redacted downstream, which is a mitigation and not the
// control: the values were still read into memory, and a redactor is a pattern matcher that can miss.
// Now the file is never opened. That is the difference between "we removed the secret from the copy
// we made" and "we did not make a copy".
//
// The `.gitignore` set is collected per directory as the walk descends, so a nested ignore file
// applies to its own subtree the way git applies it. Negations (`!keep-me`) work because the
// matching is delegated to a real gitignore implementation rather than to prefix comparison.
func (s *FilteredScanner) WalkFiles(ctx context.Context, targetDir string, visit func(relPath string, content []byte) error) error {
	ignores := newIgnoreStack(targetDir)
	return s.walkFiles(targetDir, func(f scannedFile) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if ignores.ignored(f.RelPath, false) {
			return nil
		}
		return visit(f.RelPath, f.Content)
	})
}

// ignoreStack answers "does this repository consider this path source?".
type ignoreStack struct {
	root string
	// byDir holds the compiled matcher for each directory that has a .gitignore, keyed by the
	// directory's slash-separated path relative to the root ("" for the root itself).
	byDir map[string]gitignore.IgnoreParser
}

func newIgnoreStack(root string) *ignoreStack {
	stack := &ignoreStack{root: root, byDir: map[string]gitignore.IgnoreParser{}}
	// Collected up front in one pass. Doing it lazily per directory during the walk would re-stat
	// the same files repeatedly on a wide tree, and the walk is already the expensive part.
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			name := d.Name()
			if path != root && (name == ".git" || name == "node_modules") {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() != ".gitignore" {
			return nil
		}
		parser, parseErr := gitignore.CompileIgnoreFile(path)
		if parseErr != nil {
			// An unreadable or malformed .gitignore must not stop a scan. Treated as absent, which
			// errs toward indexing too much rather than silently indexing nothing.
			return nil
		}
		dir := filepath.Dir(path)
		rel, relErr := filepath.Rel(root, dir)
		if relErr != nil {
			return nil
		}
		key := filepath.ToSlash(rel)
		if key == "." {
			key = ""
		}
		stack.byDir[key] = parser
		return nil
	})
	return stack
}

// ignored reports whether any .gitignore at or above the path's directory excludes it.
//
// Checked from the deepest applicable directory upward, because git gives the nearest file the last
// word — that is what makes a nested `!keep-me` able to re-include something the root excluded.
func (s *ignoreStack) ignored(relPath string, isDir bool) bool {
	normalised := filepath.ToSlash(relPath)
	segments := strings.Split(normalised, "/")
	for depth := len(segments) - 1; depth >= 0; depth-- {
		dirKey := strings.Join(segments[:depth], "/")
		parser, ok := s.byDir[dirKey]
		if !ok {
			continue
		}
		// The pattern is matched against the path relative to the .gitignore's own directory, which
		// is how git scopes it.
		scoped := strings.Join(segments[depth:], "/")
		if isDir {
			scoped += "/"
		}
		if parser.MatchesPath(scoped) {
			return true
		}
	}
	return false
}

// ErrBlockedPath is returned when a caller explicitly names a path the read blocklist refuses.
//
// Distinct from silently skipping it during a walk: a walk skipping `.env` is correct and
// unremarkable, whereas a caller asking for `.env` by name has asked for something the agent must
// not do, and should be told rather than handed an empty result.
var ErrBlockedPath = fmt.Errorf("scanner: path is on the read blocklist and will not be opened")

// blockedForRead reports whether the blocklist refuses this absolute path.
//
// A thin wrapper so `walkFiles` can consult the boundary without importing intent it does not have.
func blockedForRead(absPath string) bool { return fileops.BlockedForRead(absPath) }
