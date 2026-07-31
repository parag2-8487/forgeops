// SPDX-License-Identifier: Apache-2.0

// Package mutate holds the agent's ONLY file-writing code.
//
// It is importable solely from within `internal/executor/**`, by Go's nested-`internal`
// rule. That is a compile-time boundary rather than a convention: a package outside the
// executor subtree that imports this one does not build, so there is no lint to satisfy,
// no review step to remember and no discipline to maintain (§2.2.1 mechanism 3, D-45).
//
// Every entry point requires a `*envelope.Verified`, which only `envelope.Verify` can
// produce from a correctly signed envelope (D-59). So "mutate without a governance-signed
// envelope" is not a reachable state: there is no overload, no variadic escape hatch and
// no exported helper that writes.
//
// THE APPLY ALGORITHM IS PHASE 0'S, PRESERVED EXACTLY (D-45)
// Validate every path first, back up before mutating, write to a temp file in the same
// directory, fsync, chmod, rename over the target, fsync the directory, and roll every
// completed write back in reverse order on ANY error. P-08 continues to guard it, and its
// four property tests moved here with it — because a property that guards an algorithm has
// to live where the algorithm lives.
//
// What Phase 1 ADDS, all of it before or after the preserved sequence rather than inside
// it:
//   - an expected pre-image hash per entry, checked for every entry BEFORE the first
//     write, so a stale change-set aborts with ErrConflict having written nothing;
//   - write-intent path rules (`fileops.ResolveForWrite`), so generating `.env.example`
//     is permitted while `.env`, `*.pem`, `~/.ssh` and `~/.aws` stay refused (D-46);
//   - a Delete action, which Phase 0 had no concept of;
//   - a BackupManifest returned as the rollback handle, and Revert to consume it.
package mutate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/fileops"
)

// Errors returned by this package.
var (
	// ErrConflict means at least one entry's pre-image does not match its ExpectedHash.
	// The whole set is refused and nothing is written — a stale change-set must never
	// overwrite newer work (D-45, change_sets -> conflicted).
	ErrConflict = errors.New("mutate: pre-image hash mismatch; the change-set is stale")

	// ErrNoAuthority means a nil *envelope.Verified reached an entry point. The type
	// system already prevents an unverified envelope; this catches an explicit nil.
	ErrNoAuthority = errors.New("mutate: a verified envelope is required")

	// ErrHandleConsumed means this BackupManifest has already been reverted.
	ErrHandleConsumed = errors.New("mutate: this rollback handle has already been consumed")

	// ErrManifestTampered means a backup named by the manifest is missing or no longer
	// matches the hash recorded when it was taken.
	ErrManifestTampered = errors.New("mutate: a backup named by the manifest is missing or altered")

	// ErrBadEntry means an entry is internally inconsistent — a Delete carrying content,
	// a Create carrying an ExpectedHash, and so on.
	ErrBadEntry = errors.New("mutate: entry is inconsistent")
)

// Action is what an entry does to its target.
type Action string

const (
	// Create writes a file that must not already exist.
	Create Action = "create"
	// Update replaces a file that must already exist and match ExpectedHash.
	Update Action = "update"
	// Delete removes a file that must already exist and match ExpectedHash.
	Delete Action = "delete"
)

// rollbackDirName is where the consumption markers live, inside root.
//
// Inside root deliberately: Phase 0's algorithm already writes backups next to their
// targets inside root, so the footprint is consistent and a user can see the whole
// rollback state in one place. A marker in the system temp directory would not survive a
// reboot, and "the handle is single-use" would quietly become "single-use until you
// restart".
const rollbackDirName = ".forgeops-rollback"

// Entry describes one file operation within a change-set.
type Entry struct {
	// RelPath is relative to root. Absolute paths and traversal are refused.
	RelPath string

	// Action is Create, Update or Delete.
	Action Action

	// Content is the new bytes. Must be nil for Delete.
	Content []byte

	// ExpectedHash is the hex sha256 of the pre-image. Empty for Create (which asserts
	// the file does NOT exist); required for Update and Delete.
	//
	// Required rather than optional. An optional pre-image check is one a caller forgets,
	// and the consequence of forgetting is silently overwriting work the user did while
	// the change-set was awaiting approval.
	ExpectedHash string

	// Mode is the file mode for Create and Update. Zero means 0o644.
	Mode os.FileMode
}

// WrittenFile records one applied write.
type WrittenFile struct {
	RelPath    string
	AbsPath    string
	NewHash    string // hex sha256 of the bytes written; empty for Delete
	BackupPath string // absolute path of the backup, empty when there was no pre-image
	Action     Action
}

// BackupEntry is one row of the rollback handle.
type BackupEntry struct {
	RelPath string
	AbsPath string

	// BackupPath is empty exactly when the target did not exist before the apply. Revert
	// then DELETES the target rather than restoring it — Q-02's "including deleting files
	// that did not previously exist" clause.
	BackupPath string

	// PreImageHash is the hash of the pre-image, empty when there was none. Revert checks
	// the backup still matches it, so a tampered backup is a refusal rather than a
	// restore of the wrong bytes.
	PreImageHash string
}

// NoPrevious reports whether this entry had no pre-image.
//
// A method rather than a bare `BackupPath == ""` comparison at each call site, because
// this is the condition Q-02's negative control removes ("make Revert skip entries marked
// NO_PREVIOUS") and it should be one expression that a reader can find.
func (b BackupEntry) NoPrevious() bool { return b.BackupPath == "" }

// BackupManifest is the rollback handle the backend persists.
//
// Single-use, enforced durably: Revert writes a marker under root/.forgeops-rollback and
// refuses a manifest whose marker exists. Not enforced by a field on this struct, because
// the manifest is serialised to the backend and handed back — in-memory state would not
// survive the round trip, so "consumed" has to be a fact on disk.
type BackupManifest struct {
	// HandleID identifies this manifest. Derived from the envelope digest and the apply
	// time, so two applies of the same change-set produce different handles.
	HandleID string

	Root      string
	CreatedAt time.Time

	// EnvelopeDigest names the envelope that authorised the apply, so an audit row can
	// tie a revert back to the transit that created what it is undoing.
	EnvelopeDigest string

	Entries []BackupEntry
}

// ApplyReport is what ApplyVerified returns on success.
type ApplyReport struct {
	Written  []WrittenFile
	Backups  BackupManifest
	Duration time.Duration
}

// RevertReport is what Revert returns on success.
type RevertReport struct {
	Restored []string
	Removed  []string
	Duration time.Duration
}

// ApplyVerified applies a change-set atomically, or applies nothing.
//
// See the package comment for what is preserved and what is added. The ordering below is
// load-bearing and is the reason this function reads as three phases rather than one loop:
// every validation for every entry completes before the first byte is written, so a
// change-set that is going to be refused is refused with the user's disk untouched.
func ApplyVerified(
	ctx context.Context,
	v *envelope.Verified,
	root string,
	entries []Entry,
) (*ApplyReport, error) {
	started := time.Now()
	if v == nil {
		return nil, ErrNoAuthority
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(entries) == 0 {
		return nil, fmt.Errorf("%w: a change-set must contain at least one entry", ErrBadEntry)
	}

	root, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("resolve root: %w", err)
	}

	// ── Phase 1: full pre-validation, exactly as Phase 0 did, plus the hash check ──
	//
	// Phase 0 resolved and validated every path before writing anything. Phase 1 keeps
	// that and adds the pre-image comparison to the same phase rather than to the write
	// loop, because a mismatch discovered halfway through would mean rolling back writes
	// that were never in question.
	absPaths := make([]string, len(entries))
	preImages := make([]string, len(entries))
	seen := make(map[string]int, len(entries))
	for i, e := range entries {
		if err := validateEntryShape(e); err != nil {
			return nil, err
		}
		abs, err := fileops.ResolveForWrite(root, e.RelPath)
		if err != nil {
			return nil, err
		}
		// Two entries targeting one file would make "either all new content or all
		// pre-images" ill-defined: whichever wrote last would win and the other's backup
		// would hold an intermediate state. Refused rather than ordered.
		if first, duplicate := seen[abs]; duplicate {
			return nil, fmt.Errorf("%w: entries %d and %d both target %s", ErrBadEntry, first, i, e.RelPath)
		}
		seen[abs] = i
		absPaths[i] = abs

		hash, exists, err := hashFile(abs)
		if err != nil {
			return nil, err
		}
		preImages[i] = hash

		switch e.Action {
		case Create:
			if exists {
				return nil, fmt.Errorf("%w: %s already exists but the entry is a create",
					ErrConflict, e.RelPath)
			}
		case Update, Delete:
			if !exists {
				return nil, fmt.Errorf("%w: %s does not exist but the entry is a %s",
					ErrConflict, e.RelPath, e.Action)
			}
			if hash != e.ExpectedHash {
				return nil, fmt.Errorf("%w: %s has pre-image %s, the change-set expected %s",
					ErrConflict, e.RelPath, hash, e.ExpectedHash)
			}
		}
	}

	digest := v.Digest()
	manifest := BackupManifest{
		HandleID:       handleID(digest, started),
		Root:           root,
		CreatedAt:      started.UTC(),
		EnvelopeDigest: digest,
		Entries:        make([]BackupEntry, 0, len(entries)),
	}

	backups := make([]backupInfo, 0, len(entries))
	written := make([]string, 0, len(entries))

	// ── Phase 2: write with backups — Phase 0's loop, unchanged in sequence ──
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
			backupPath := abs + ".backup." + started.UTC().Format("20060102T150405Z")
			if err := copyFile(abs, backupPath); err != nil {
				rollback(written, backups)
				return nil, fmt.Errorf("backup %s: %w", abs, err)
			}
			bi = backupInfo{path: backupPath, existed: true}
		}
		backups = append(backups, bi)
		manifest.Entries = append(manifest.Entries, BackupEntry{
			RelPath:      e.RelPath,
			AbsPath:      abs,
			BackupPath:   bi.path,
			PreImageHash: preImages[i],
		})

		if e.Action == Delete {
			if err := os.Remove(abs); err != nil {
				rollback(written, backups)
				return nil, fmt.Errorf("delete %s: %w", e.RelPath, err)
			}
			fsyncDir(dir)
			written = append(written, abs)
			continue
		}

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

	// ── Phase 3: report ──
	report := &ApplyReport{
		Written:  make([]WrittenFile, 0, len(entries)),
		Backups:  manifest,
		Duration: time.Since(started),
	}
	for i, e := range entries {
		newHash := ""
		if e.Action != Delete {
			newHash = hashBytes(e.Content)
		}
		report.Written = append(report.Written, WrittenFile{
			RelPath:    e.RelPath,
			AbsPath:    absPaths[i],
			NewHash:    newHash,
			BackupPath: manifest.Entries[i].BackupPath,
			Action:     e.Action,
		})
	}
	return report, nil
}

// Revert restores every file named by a manifest to its pre-image.
//
// It is itself a mutation and therefore also requires a verified envelope with its own
// approval — a rollback changes the user's disk and gets the same scrutiny as the apply
// did (§10.5). `GovernanceChokepoint.revert` mints a FRESH authority rather than reusing
// the original, which is why this takes a `*envelope.Verified` and not the manifest alone.
//
// Order: restore in REVERSE, for the same reason the apply's rollback does. If a later
// entry created a directory an earlier one now needs removed, undoing forwards leaves
// the tree in a state neither the pre-image nor the post-image describes.
func Revert(ctx context.Context, v *envelope.Verified, m BackupManifest) (*RevertReport, error) {
	started := time.Now()
	if v == nil {
		return nil, ErrNoAuthority
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if m.HandleID == "" || m.Root == "" {
		return nil, fmt.Errorf("%w: the manifest names no handle or no root", ErrBadEntry)
	}

	marker := filepath.Join(m.Root, rollbackDirName, m.HandleID+".consumed")
	if _, err := os.Stat(marker); err == nil {
		// Idempotent in EFFECT and single-use in AUTHORITY: the filesystem is already at
		// the pre-image, and this call changes nothing. Both halves of Q-02's clause hold
		// at once, which they could not if a second revert silently "succeeded" and did
		// a second round of restores from backups that no longer describe the current
		// state.
		return nil, fmt.Errorf("%w: %s", ErrHandleConsumed, m.HandleID)
	}

	// Full pre-validation before touching anything, the same discipline as apply: every
	// backup must exist and still match the hash recorded when it was taken.
	for _, entry := range m.Entries {
		if entry.NoPrevious() {
			continue
		}
		hash, exists, err := hashFile(entry.BackupPath)
		if err != nil {
			return nil, err
		}
		if !exists {
			return nil, fmt.Errorf("%w: %s is gone", ErrManifestTampered, entry.BackupPath)
		}
		if entry.PreImageHash != "" && hash != entry.PreImageHash {
			return nil, fmt.Errorf("%w: %s hashes %s, the manifest recorded %s",
				ErrManifestTampered, entry.BackupPath, hash, entry.PreImageHash)
		}
	}

	report := &RevertReport{}
	for i := len(m.Entries) - 1; i >= 0; i-- {
		// One step per entry, in `revert_entry.go`. Extracted so Q-02's negative control is a
		// three-line overlay rather than a copy of this file; see the note there.
		if err := revertOne(m.Entries[i], report); err != nil {
			return nil, err
		}
	}

	// Mark consumed only after every restore succeeded. Marking first would make a
	// partial revert unrepeatable, which is the worst of both behaviours.
	if err := writeMarker(marker, m); err != nil {
		return nil, err
	}
	sort.Strings(report.Restored)
	sort.Strings(report.Removed)
	report.Duration = time.Since(started)
	return report, nil
}

// validateEntryShape refuses an entry whose fields contradict its action.
//
// Checked before anything is resolved, because these are statements about the entry
// rather than about the filesystem, and a caller that got them wrong has a bug the
// filesystem cannot diagnose.
func validateEntryShape(e Entry) error {
	if e.RelPath == "" {
		return fmt.Errorf("%w: RelPath is empty", ErrBadEntry)
	}
	switch e.Action {
	case Create:
		if e.ExpectedHash != "" {
			return fmt.Errorf("%w: %s is a create and carries an ExpectedHash; a create asserts "+
				"the file does not exist, so there is no pre-image to expect", ErrBadEntry, e.RelPath)
		}
	case Update:
		if e.ExpectedHash == "" {
			return fmt.Errorf("%w: %s is an update with no ExpectedHash; without one a stale "+
				"change-set silently overwrites newer work", ErrBadEntry, e.RelPath)
		}
	case Delete:
		if e.ExpectedHash == "" {
			return fmt.Errorf("%w: %s is a delete with no ExpectedHash", ErrBadEntry, e.RelPath)
		}
		if e.Content != nil {
			return fmt.Errorf("%w: %s is a delete and carries content", ErrBadEntry, e.RelPath)
		}
	default:
		return fmt.Errorf("%w: %s has unknown action %q", ErrBadEntry, e.RelPath, e.Action)
	}
	return nil
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

// hashFile returns the hex sha256 of path's contents and whether it exists.
func hashFile(path string) (string, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", false, nil
		}
		return "", false, fmt.Errorf("read %s: %w", path, err)
	}
	return hashBytes(data), true, nil
}

func hashBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// handleID derives a manifest identifier from the envelope digest and the apply instant.
//
// Both, not just the digest: applying the same change-set twice is legitimate (the second
// apply's pre-images are the first apply's outputs), and two applies must not share a
// single-use handle.
func handleID(envelopeDigest string, at time.Time) string {
	sum := sha256.Sum256([]byte(envelopeDigest + "|" + at.UTC().Format(time.RFC3339Nano)))
	return hex.EncodeToString(sum[:16])
}

// writeMarker records that a handle has been consumed.
func writeMarker(marker string, m BackupManifest) error {
	if err := os.MkdirAll(filepath.Dir(marker), 0o700); err != nil {
		return fmt.Errorf("create rollback state directory: %w", err)
	}
	body := fmt.Sprintf("handle=%s\nenvelope_digest=%s\napplied_at=%s\nreverted_at=%s\n",
		m.HandleID, m.EnvelopeDigest, m.CreatedAt.Format(time.RFC3339), time.Now().UTC().Format(time.RFC3339))
	if err := os.WriteFile(marker, []byte(body), 0o600); err != nil {
		return fmt.Errorf("write rollback marker: %w", err)
	}
	fsyncDir(filepath.Dir(marker))
	return nil
}
