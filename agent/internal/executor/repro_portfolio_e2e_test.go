// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestReproPortfolioEndToEnd is the original failure reproduced and resolved against the REAL
// repository that produced it.
//
// The report was:
//
//	docker compose up failed for docker-compose.yml:
//	#11 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 127
//
// Exit 127 is "command not found", and inside that Dockerfile the command that was not found is `tsc`:
// the builder stage ran `npm install --production`, which omits devDependencies, and `tsc` and `vite`
// live there. The CMD named `dist/server.js` on top of that, which a static SPA never produces.
//
// A static assertion that the healer rewrites those two lines is not the same claim as the container
// actually building, so this runs the real thing. Opt in with FORGEOPS_PORTFOLIO_E2E=1; it needs a
// docker daemon and the `node:22-slim` base image.
func TestReproPortfolioEndToEnd(t *testing.T) {
	if os.Getenv("FORGEOPS_PORTFOLIO_E2E") != "1" {
		t.Skip("set FORGEOPS_PORTFOLIO_E2E=1 to build the real repository")
	}
	if _, err := os.Stat(reproSourceDir); err != nil {
		t.Skipf("repro corpus absent: %v", err)
	}
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("docker not on PATH")
	}

	// The repository is COPIED, never modified: the point of the exercise is that ForgeOps adapts to
	// an application it does not own, and editing the original would be the one-off fix this whole
	// change exists to avoid.
	tmp := t.TempDir()
	copyReproCorpus(t, reproSourceDir, tmp)

	before := readFileOrEmpty(filepath.Join(tmp, "Dockerfile"))
	if !strings.Contains(before, "--production") {
		t.Fatalf("the reproduction corpus no longer carries the failing Dockerfile:\n%s", before)
	}

	sink := SinkFunc(func(percent int, stage, message string) {})
	profile, buildable := ensureBuildableArtifacts(tmp, sink)
	if !buildable {
		t.Fatalf("ForgeOps reported the repository unbuildable: %s", profile.PrimaryLanguage)
	}

	after := readFileOrEmpty(filepath.Join(tmp, "Dockerfile"))
	t.Logf("healed Dockerfile:\n%s", after)

	cmd := exec.Command("docker", "compose", "-p", "forgeops-repro-portfolio", "build")
	cmd.Dir = tmp
	out, err := cmd.CombinedOutput()
	t.Cleanup(func() {
		cleanup := exec.Command("docker", "compose", "-p", "forgeops-repro-portfolio", "down", "--rmi", "local", "-v")
		cleanup.Dir = tmp
		_ = cleanup.Run()
	})

	message := string(out)
	if err != nil {
		if strings.Contains(message, "TLS handshake timeout") || strings.Contains(message, "failed to do request") {
			t.Skipf("registry unreachable, cannot attribute this to the platform:\n%s", tail(message, 15))
		}
		t.Fatalf("the original failure is NOT resolved; the build still fails:\n%s", tail(message, 60))
	}

	// The build succeeded AND the fix is the general one: no application-specific branch was needed,
	// and the repository on disk is untouched.
	if strings.Contains(message, "exit code: 127") {
		t.Errorf("exit code 127 survived:\n%s", tail(message, 30))
	}
	if readFileOrEmpty(filepath.Join(reproSourceDir, "Dockerfile")) != before {
		t.Errorf("the source repository was modified; ForgeOps must adapt to it, not edit it")
	}

	// A CONTAINER THAT BUILDS AND DOES NOT RUN HAS NOT BEEN DEPLOYED. The original Dockerfile's CMD
	// named `dist/server.js`, which a static SPA never produces, so the image built AND exited 127 at
	// start — a second instance of the same fault one stage later. Only starting it proves absent.
	up := exec.Command("docker", "compose", "-p", "forgeops-repro-portfolio", "up", "-d", "--wait")
	up.Dir = tmp
	upOut, upErr := up.CombinedOutput()
	if upErr != nil {
		ps, _ := exec.Command("docker", "compose", "-p", "forgeops-repro-portfolio", "logs", "--tail", "30").CombinedOutput()
		t.Fatalf("the image built but the container did not come up:\n%s\n--- logs ---\n%s",
			tail(string(upOut), 25), tail(string(ps), 30))
	}
	t.Logf("container came up healthy:\n%s", tail(string(upOut), 10))
}

// copyReproCorpus mirrors a real checkout into a scratch directory, skipping the parts that are large
// or irrelevant to a container build.
//
// It also MATERIALISES TRACKED-BUT-DELETED FILES. The corpus repository has one on disk —
// `vite.config.ts` is tracked and absent from the working tree — and a missing config file makes `tsc -b`
// fail for a reason that has nothing to do with deployment. Deploying from a repository means deploying
// what the repository actually contains, so the copy is completed from the object store first and the
// build is judged on the application rather than on an unfinished checkout.
func copyReproCorpus(t *testing.T, src, dst string) {
	t.Helper()
	skip := map[string]bool{".git": true, "node_modules": true, ".next": true, "dist": true, "build": true}

	var walk func(rel string)
	walk = func(rel string) {
		entries, err := os.ReadDir(filepath.Join(src, rel))
		if err != nil {
			t.Fatalf("read %s: %v", rel, err)
		}
		for _, e := range entries {
			if skip[e.Name()] {
				continue
			}
			child := filepath.Join(rel, e.Name())
			if e.IsDir() {
				if err := os.MkdirAll(filepath.Join(dst, child), 0755); err != nil {
					t.Fatal(err)
				}
				walk(child)
				continue
			}
			raw, err := os.ReadFile(filepath.Join(src, child))
			if err != nil {
				t.Fatalf("read %s: %v", child, err)
			}
			if err := os.WriteFile(filepath.Join(dst, child), raw, 0644); err != nil {
				t.Fatal(err)
			}
		}
	}
	walk(".")

	tracked, err := exec.Command("git", "-C", src, "ls-files").Output()
	if err != nil {
		return // Not a checkout, or no git available: the working tree is all there is.
	}
	for _, rel := range strings.Split(strings.TrimSpace(string(tracked)), "\n") {
		rel = strings.TrimSpace(rel)
		if rel == "" {
			continue
		}
		target := filepath.Join(dst, filepath.FromSlash(rel))
		if _, err := os.Stat(target); err == nil {
			continue
		}
		blob, err := exec.Command("git", "-C", src, "show", "HEAD:"+rel).Output()
		if err != nil {
			continue // Staged-but-never-committed, or otherwise unavailable.
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(target, blob, 0644); err != nil {
			t.Fatal(err)
		}
		t.Logf("restored tracked-but-deleted file into the copy: %s", rel)
	}
}
