// SPDX-License-Identifier: Apache-2.0

package fileops

import (
	"os"
	"path/filepath"
	"strings"
)

// Path blocklists, split by intent (design §7.11(f), §17.1 D-46).
//
// Why two lists rather than one
// -----------------------------
// Phase 0 had a single blocklist, and it was right for reading: the agent must never
// read `.env`, `~/.ssh`, `~/.aws` or a `.pem`, because those values would then be
// available to put in a prompt. §1.5 needs the agent to WRITE a small, closed set of
// example files — a generated `.env.example` is one of the artifacts the platform
// produces — so the write list is the read list plus exactly three permitted names.
//
// Read strictness is unchanged. `blockedForWrite` is strictly more permissive than
// `blockedForRead` on three names and identical everywhere else, which is asserted
// directly rather than left to inspection.
//
// Why a closed list of names and never a glob
// ------------------------------------------
// A pattern like `.env.*example*` or `*.example` looks equivalent and is not.
// `.env.production.example.bak` matches such a glob, and it is a backup of a real
// production environment file — exactly the thing that must stay unwritable. Three
// exact names cannot be widened by accident; a glob can be widened by a filename
// somebody else chooses.

// writableExemptions are the ONLY names the write blocklist permits that the read
// blocklist refuses.
//
// Matched EXACTLY, including case, while `blockedForRead` folds case for the `.env`
// family. That asymmetry is deliberate and is the safe direction: `.ENV.EXAMPLE` stays
// refused for writing, because the agent generates the canonical lowercase name and a
// case variant arriving from anywhere else is more likely to be an attempt to slip past
// the rule than a legitimate artifact.
//
// Deliberately not a variable a caller can append to: the set is the security boundary,
// so it is a compile-time constant list in one place.
var writableExemptions = [...]string{
	".env.example",
	".env.sample",
	".env.template",
}

// blockedForRead reports whether reading absPath is refused.
//
// Phase 0's `isBlocked`, unchanged in behaviour and moved here so the two intents sit
// side by side and a future edit cannot change one while believing it changed both.
func blockedForRead(absPath string) bool {
	norm := filepath.Clean(absPath)

	if home, err := os.UserHomeDir(); err == nil && home != "" {
		for _, sensitive := range []string{".ssh", ".aws"} {
			dir := filepath.Clean(filepath.Join(home, sensitive))
			if norm == dir || strings.HasPrefix(norm, dir+string(filepath.Separator)) {
				return true
			}
		}
	}

	// Case-folded. This compared `.env` and the `.env.` prefix case-SENSITIVELY, so
	// `.ENV.PRODUCTION` was not blocked at all — and on Windows and macOS, where the
	// filesystem is case-insensitive by default, that is the SAME FILE as
	// `.env.production`. The blocklist could therefore be bypassed by changing the
	// case of a filename. The `.pem` check below already folded case; the `.env` family
	// did not, and nothing had asked.
	base := strings.ToLower(filepath.Base(norm))
	if base == ".env" || strings.HasPrefix(base, ".env.") {
		return true
	}
	if strings.HasSuffix(base, ".pem") {
		return true
	}
	return false
}

// blockedForWrite reports whether writing absPath is refused.
//
// Identical to blockedForRead except that the three names in writableExemptions are
// permitted. The exemption is checked on the BASE NAME only, so a directory called
// `.env.example/` containing other files gains nothing: each file inside is judged on
// its own name.
func blockedForWrite(absPath string) bool {
	base := filepath.Base(filepath.Clean(absPath))
	for _, permitted := range writableExemptions {
		// CASE-SENSITIVE, while `blockedForRead` case-FOLDS, and the asymmetry is deliberate.
		//
		// On Windows and macOS the filesystem is case-insensitive by default, so `.ENV.EXAMPLE` and
		// `.env.example` are the same file. Folding here would widen the exemption to every casing;
		// not folding means an oddly-cased name misses the exemption and is then caught by
		// `blockedForRead`, which does fold. So the asymmetry fails CLOSED: the worst outcome is
		// refusing to write a file that would have been allowed, and the caller is told which rule
		// refused it. Folding both would make the exemption the wider of the two rules, which is the
		// direction that cannot be safe.
		if base == permitted {
			// Still refuse if the path is inside a sensitive directory: a file named
			// `.env.example` under ~/.ssh is not an example file, it is a way into
			// ~/.ssh. The exemption widens the NAME rule, never the directory rule.
			return blockedDirectory(absPath)
		}
	}
	return blockedForRead(absPath)
}

// blockedDirectory reports whether absPath lies inside a directory that is refused
// regardless of filename.
func blockedDirectory(absPath string) bool {
	norm := filepath.Clean(absPath)
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return false
	}
	for _, sensitive := range []string{".ssh", ".aws"} {
		dir := filepath.Clean(filepath.Join(home, sensitive))
		if norm == dir || strings.HasPrefix(norm, dir+string(filepath.Separator)) {
			return true
		}
	}
	return false
}
