// SPDX-License-Identifier: Apache-2.0

package fileops

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The intent-split blocklist (design §7.11(f), §17.1 D-46, Q-01).
//
// The matrix is enumerated rather than generated, and every row states the outcome for
// BOTH intents. That shape is deliberate: the interesting property is not "`.env` is
// blocked" but "the write list differs from the read list on exactly three names", and a
// per-intent test pair would not show the difference at a glance.

func TestBlocklist_IntentMatrix(t *testing.T) {
	t.Parallel()

	cases := []struct {
		path        string
		blockRead   bool
		blockWrite  bool
		explanation string
	}{
		{
			path: ".env", blockRead: true, blockWrite: true,
			explanation: "the real environment file: never readable, never writable",
		},
		{
			path: ".env.local", blockRead: true, blockWrite: true,
			explanation: "a developer's local overrides hold real values",
		},
		{
			path: ".env.production", blockRead: true, blockWrite: true,
			explanation: "the most sensitive of all",
		},
		{
			path: ".env.example", blockRead: true, blockWrite: false,
			explanation: "an artifact §1.5 generates; placeholder values only",
		},
		{
			path: ".env.sample", blockRead: true, blockWrite: false,
			explanation: "the same file under its other conventional name",
		},
		{
			path: ".env.template", blockRead: true, blockWrite: false,
			explanation: "and its third conventional name",
		},
		{
			path: ".env.example.bak", blockRead: true, blockWrite: true,
			explanation: "a BACKUP of something; a glob would have permitted this",
		},
		{
			path: ".env.production.example.bak", blockRead: true, blockWrite: true,
			explanation: "the case D-46 names explicitly: contains 'example' and is a prod backup",
		},
		{
			path: ".envrc", blockRead: false, blockWrite: false,
			explanation: "direnv config, not an env file; the prefix rule requires the dot",
		},
		{
			path: "sub/.env", blockRead: true, blockWrite: true,
			explanation: "the rule is on the base name, so nesting changes nothing",
		},
		{
			path: "sub/.env.example", blockRead: true, blockWrite: false,
			explanation: "and the exemption is on the base name too",
		},
		{
			path: "server.pem", blockRead: true, blockWrite: true,
			explanation: "key material, either direction",
		},
		{
			path: "SERVER.PEM", blockRead: true, blockWrite: true,
			explanation: "the .pem check folds case; a shouty filename is still key material",
		},
		{
			path: "Dockerfile", blockRead: false, blockWrite: false,
			explanation: "an ordinary generated artifact",
		},
		{
			path: "k8s/deployment.yaml", blockRead: false, blockWrite: false,
			explanation: "an ordinary generated artifact in a subdirectory",
		},
		{
			path: "environment.txt", blockRead: false, blockWrite: false,
			explanation: "starts with 'env' but is not `.env`; the rule must not over-match",
		},
	}

	root := t.TempDir()

	for _, c := range cases {
		t.Run(c.path, func(t *testing.T) {
			t.Parallel()
			abs := filepath.Join(root, filepath.FromSlash(c.path))

			if got := blockedForRead(abs); got != c.blockRead {
				t.Errorf("blockedForRead(%q) = %v, want %v — %s", c.path, got, c.blockRead, c.explanation)
			}
			if got := blockedForWrite(abs); got != c.blockWrite {
				t.Errorf("blockedForWrite(%q) = %v, want %v — %s", c.path, got, c.blockWrite, c.explanation)
			}
		})
	}
}

func TestBlocklist_WriteIsReadPlusExactlyThreeNames(t *testing.T) {
	t.Parallel()

	// The structural claim, asserted rather than inspected: the write list is the read
	// list with three exemptions and no other difference. If a future edit relaxes the
	// write rule anywhere else, this fails even if the matrix above was not updated.
	if len(writableExemptions) != 3 {
		t.Fatalf("writableExemptions has %d entries, want exactly 3: %v", len(writableExemptions), writableExemptions)
	}

	root := t.TempDir()
	exempt := map[string]bool{}
	for _, name := range writableExemptions {
		exempt[name] = true
	}

	// A broad sample of names, including every exemption and many near-misses.
	names := []string{
		".env", ".env.local", ".env.production", ".env.test", ".env.ci",
		".env.example", ".env.sample", ".env.template",
		".env.example.bak", ".env.sample.old", ".env.templates", ".env.exampl",
		".envrc", "env", "environment", "server.pem", "key.PEM",
		"Dockerfile", "compose.yaml", "main.go", "README.md",
	}

	for _, name := range names {
		abs := filepath.Join(root, name)
		read, write := blockedForRead(abs), blockedForWrite(abs)
		switch {
		case exempt[name]:
			if !read || write {
				t.Errorf("%q: want read=true write=false, got read=%v write=%v", name, read, write)
			}
		default:
			if read != write {
				t.Errorf("%q: the two intents must agree for any non-exempt name, got read=%v write=%v",
					name, read, write)
			}
		}
	}
}

func TestBlocklist_ExemptionsAreExactNamesNotPatterns(t *testing.T) {
	t.Parallel()

	// D-46's reasoning, made executable. Any name that merely CONTAINS an exemption
	// must stay blocked for writing.
	//
	// `"." + permitted` is deliberately NOT in this list. `..env.example` is not an env
	// file under any reading — it does not match the `.env.` prefix — so refusing it for
	// writing would make the write rule stricter than the read rule, contradicting the
	// "write = read + exactly three names" property asserted above. Discovered by this
	// test failing: the expectation was wrong, not the code.
	root := t.TempDir()
	for _, permitted := range writableExemptions {
		for _, variant := range []string{
			permitted + ".bak",
			permitted + ".old",
			permitted + "~",
			strings.ToUpper(permitted),
			permitted + ".gpg",
		} {
			if !blockedForWrite(filepath.Join(root, variant)) {
				t.Errorf("%q is writable; only the exact name %q may be", variant, permitted)
			}
		}
	}
}

func TestBlocklist_TheEnvFamilyMatchFoldsCase(t *testing.T) {
	t.Parallel()

	// A real gap this leaf found. The `.env` and `.env.` checks compared case
	// SENSITIVELY, so `.ENV.PRODUCTION` was not blocked at all — and on Windows and
	// macOS, where the filesystem is case-insensitive by default, that is the SAME FILE
	// as `.env.production`. The blocklist could be bypassed by changing a filename's
	// case. The `.pem` check already folded case; this one did not, and no test had
	// asked.
	root := t.TempDir()
	for _, name := range []string{
		".ENV", ".Env", ".eNv",
		".ENV.PRODUCTION", ".Env.Local", ".ENV.example",
		"SERVER.PEM", "server.Pem",
	} {
		abs := filepath.Join(root, name)
		if !blockedForRead(abs) {
			t.Errorf("blockedForRead(%q) = false; case folding is missing", name)
		}
		if !blockedForWrite(abs) {
			t.Errorf("blockedForWrite(%q) = false; a case variant must not be writable", name)
		}
	}
}

func TestBlocklist_AnExemptNameInsideASensitiveDirectoryIsStillRefused(t *testing.T) {
	t.Parallel()

	// The exemption widens the NAME rule, never the directory rule. A file called
	// `.env.example` under ~/.ssh is not an example file; it is a way to write into
	// ~/.ssh.
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		t.Skip("no home directory on this platform")
	}

	for _, sensitive := range []string{".ssh", ".aws"} {
		for _, permitted := range writableExemptions {
			abs := filepath.Join(home, sensitive, permitted)
			if !blockedForWrite(abs) {
				t.Errorf("%s is writable; the directory rule must survive the name exemption", abs)
			}
		}
	}
}

func TestBlocklist_SensitiveDirectoriesAreRefusedInBothDirections(t *testing.T) {
	t.Parallel()

	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		t.Skip("no home directory on this platform")
	}

	for _, sensitive := range []string{".ssh", ".aws"} {
		dir := filepath.Join(home, sensitive)
		for _, path := range []string{dir, filepath.Join(dir, "id_ed25519"), filepath.Join(dir, "config")} {
			if !blockedForRead(path) {
				t.Errorf("blockedForRead(%q) = false", path)
			}
			if !blockedForWrite(path) {
				t.Errorf("blockedForWrite(%q) = false", path)
			}
		}
	}
}

func TestBlocklist_ReadStrictnessIsUnchangedFromPhase0(t *testing.T) {
	t.Parallel()

	// Phase 0's `isBlocked` alias is gone: D-45 moved the write path out of this package,
	// so the two intents now have exported resolvers of their own and there is no longer
	// a single "the" blocklist to alias. The assertion is unchanged in substance — the
	// READ path must consult `blockedForRead` and nothing else — and is now made against
	// `ResolveForRead`, which is what callers actually use.
	root := t.TempDir()
	for _, name := range []string{".env", ".env.example", ".env.production", "server.pem"} {
		abs := filepath.Join(root, name)
		_, err := ResolveForRead(root, name)
		refusedByResolver := errors.Is(err, ErrPathBlocked)
		if refusedByResolver != blockedForRead(abs) {
			t.Errorf("ResolveForRead and blockedForRead disagree on %q (resolver refused=%v)",
				name, refusedByResolver)
		}
	}
	// And specifically: reading an example file is still refused. The write exemption
	// must not have leaked into the read path, because a readable `.env.example` in a
	// project that (wrongly) put real values in it would reach a prompt.
	if _, err := ResolveForRead(root, ".env.example"); !errors.Is(err, ErrPathBlocked) {
		t.Error(".env.example became readable; the write exemption leaked into the read path")
	}
	// The counterpart, which is the whole reason D-46 split the list: the same name IS
	// writable. Asserting both here means the pair cannot drift in one direction only.
	if _, err := ResolveForWrite(root, ".env.example"); err != nil {
		t.Errorf(".env.example must be writable (D-46, §1.5 lists it as a generated artifact): %v", err)
	}
	if _, err := ResolveForWrite(root, ".env"); !errors.Is(err, ErrPathBlocked) {
		t.Error(".env must never be writable")
	}
}
