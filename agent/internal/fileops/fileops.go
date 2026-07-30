// SPDX-License-Identifier: Apache-2.0
package fileops

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/sergi/go-diff/diffmatchpatch"
)

// Errors
var (
	ErrPathOutsideRoot = errors.New("path resolves outside root")
	ErrPathBlocked     = errors.New("path matches blocklist")
)

// Ops is the file operations interface.
type Ops interface {
	ApplyAtomic(ctx context.Context, root string, entries []WriteEntry) (*ApplyReport, error)
	UnifiedDiff(before, after, label string) string
}

// WriteEntry describes a single file to write.
type WriteEntry struct {
	RelPath string
	Content []byte
	Mode    os.FileMode
}

// ApplyReport records what was written and backed up.
type ApplyReport struct {
	Written []string
	Backups []string
}

// FileOps implements Ops.
type FileOps struct{}

// New creates a new FileOps instance.
func New() *FileOps {
	return &FileOps{}
}

// ApplyAtomic writes every entry or none. For each target it first writes a
// timestamped backup, then writes to a temp file in the same directory,
// fsyncs, and renames over the target. On any error it rolls back.
func (f *FileOps) ApplyAtomic(_ context.Context, root string, entries []WriteEntry) (*ApplyReport, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("resolve root: %w", err)
	}

	// Phase 1: Full pre-validation
	absPaths := make([]string, len(entries))
	for i, e := range entries {
		abs, err := resolveAndValidate(root, e.RelPath)
		if err != nil {
			return nil, err
		}
		absPaths[i] = abs
	}

	backups := make([]backupInfo, 0, len(entries))
	written := make([]string, 0, len(entries))

	// Phase 2: Write with backups
	for i, e := range entries {
		abs := absPaths[i]

		// Ensure parent directory exists
		dir := filepath.Dir(abs)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			rollback(written, backups)
			return nil, fmt.Errorf("mkdir %s: %w", dir, err)
		}

		// Backup if exists
		var bi backupInfo
		if _, statErr := os.Stat(abs); statErr == nil {
			backupPath := abs + ".backup." + time.Now().Format("20060102T150405Z")
			if err := copyFile(abs, backupPath); err != nil {
				rollback(written, backups)
				return nil, fmt.Errorf("backup %s: %w", abs, err)
			}
			bi = backupInfo{path: backupPath, existed: true}
		}
		backups = append(backups, bi)

		// Write to temp file in same directory
		mode := e.Mode
		if mode == 0 {
			mode = 0o644
		}
		tmp, err := os.CreateTemp(dir, ".forgeops-*")
		if err != nil {
			rollback(written, backups)
			return nil, fmt.Errorf("create temp for %s: %w", e.RelPath, err)
		}
		tmpName := tmp.Name()

		if _, err := tmp.Write(e.Content); err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
			rollback(written, backups)
			return nil, fmt.Errorf("write %s: %w", e.RelPath, err)
		}

		// fsync the file
		if err := tmp.Sync(); err != nil {
			_ = tmp.Close()
			_ = os.Remove(tmpName)
			rollback(written, backups)
			return nil, fmt.Errorf("fsync %s: %w", e.RelPath, err)
		}
		_ = tmp.Close()

		if err := os.Chmod(tmpName, mode); err != nil {
			_ = os.Remove(tmpName)
			rollback(written, backups)
			return nil, fmt.Errorf("chmod %s: %w", e.RelPath, err)
		}

		// Atomic rename
		if err := os.Rename(tmpName, abs); err != nil {
			_ = os.Remove(tmpName)
			rollback(written, backups)
			return nil, fmt.Errorf("rename %s: %w", e.RelPath, err)
		}

		// fsync the directory
		fsyncDir(dir)

		written = append(written, abs)
	}

	// Collect backup paths for report
	var backupPaths []string
	for _, b := range backups {
		if b.existed {
			backupPaths = append(backupPaths, b.path)
		}
	}

	return &ApplyReport{
		Written: written,
		Backups: backupPaths,
	}, nil
}

// UnifiedDiff produces a unified diff string using sergi/go-diff.
func (f *FileOps) UnifiedDiff(before, after, label string) string {
	dmp := diffmatchpatch.New()
	a, b, c := dmp.DiffLinesToChars(before, after)
	diffs := dmp.DiffMain(a, b, false)
	diffs = dmp.DiffCharsToLines(diffs, c)
	diffs = dmp.DiffCleanupSemantic(diffs)

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("--- a/%s\n+++ b/%s\n", label, label))
	for _, d := range diffs {
		lines := strings.Split(d.Text, "\n")
		for _, line := range lines {
			if line == "" {
				continue
			}
			switch d.Type {
			case diffmatchpatch.DiffDelete:
				sb.WriteString("-" + line + "\n")
			case diffmatchpatch.DiffInsert:
				sb.WriteString("+" + line + "\n")
			case diffmatchpatch.DiffEqual:
				sb.WriteString(" " + line + "\n")
			}
		}
	}
	return sb.String()
}

// resolveAndValidate checks a path is within root and not blocklisted.
func resolveAndValidate(root, relPath string) (string, error) {
	// Clean the path
	cleaned := filepath.Clean(relPath)
	if filepath.IsAbs(cleaned) {
		return "", fmt.Errorf("%w: %s is absolute", ErrPathOutsideRoot, relPath)
	}

	joined := filepath.Join(root, cleaned)

	// Resolve symlinks for containment check
	resolved, err := filepath.EvalSymlinks(filepath.Dir(joined))
	if err != nil {
		// If parent doesn't exist yet, resolve what we can
		resolved, err = filepath.EvalSymlinks(root)
		if err != nil {
			return "", fmt.Errorf("resolve root: %w", err)
		}
		joined = filepath.Join(resolved, cleaned)
	} else {
		joined = filepath.Join(resolved, filepath.Base(joined))
	}

	// Root containment check
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", fmt.Errorf("resolve root: %w", err)
	}

	// Normalize for comparison
	resolvedRootNorm := filepath.Clean(resolvedRoot) + string(filepath.Separator)
	joinedNorm := filepath.Clean(joined)

	if !strings.HasPrefix(joinedNorm+string(filepath.Separator), resolvedRootNorm) &&
		joinedNorm != filepath.Clean(resolvedRoot) {
		return "", fmt.Errorf("%w: %s escapes root %s", ErrPathOutsideRoot, relPath, root)
	}

	// Blocklist check
	if isBlocked(joined) {
		return "", fmt.Errorf("%w: %s", ErrPathBlocked, relPath)
	}

	return joined, nil
}

// isBlocked checks PRD 2.2 blocklist: ~/.ssh, ~/.aws, .env, *.pem
//
// The implementation moved to blocklist.go as `blockedForRead` when task 4.7 split the
// list by intent (§7.11(f), D-46). Kept as a one-line alias rather than rewriting every
// call site, because `resolveAndValidate` is on the READ path and its strictness must
// not change — P-08's read clause asserts exactly this behaviour.
func isBlocked(absPath string) bool {
	return blockedForRead(absPath)
}

type backupInfo struct {
	path    string
	existed bool
}

func rollback(written []string, backups []backupInfo) {
	// Roll back in reverse order
	for i := len(written) - 1; i >= 0; i-- {
		if i < len(backups) && backups[i].existed {
			// Restore from backup
			_ = os.Rename(backups[i].path, written[i])
		} else {
			// Remove if it didn't exist before
			_ = os.Remove(written[i])
		}
	}
}

func copyFile(src, dst string) error {
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	return os.WriteFile(dst, data, info.Mode())
}

func fsyncDir(dir string) {
	d, err := os.Open(dir)
	if err != nil {
		return
	}
	_ = d.Sync()
	_ = d.Close()
}
