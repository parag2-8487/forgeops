// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// reproSourceDir is the real Vite/React portfolio that failed with
// `npm run build ... exit code: 127`. It exists only to reproduce; the test skips when absent.
// reproSourceDir is the real Vite/React portfolio that failed with
// `npm run build ... exit code: 127`. It exists only to reproduce; the test skips when absent.
const reproSourceDir = `C:\Users\ASUS\Downloads\for testing\Portfolio`

// reproBuggyDockerfile is the Dockerfile that PRODUCED the reported failure, pinned here rather than
// read from the workspace.
//
// It cannot be read from the workspace any more, and that is the point: ForgeOps healed that file in
// place on the first successful deployment, so the copy on disk is the healed one. A reproduction that
// reads its own inputs from a tree the code under test has already rewritten stops reproducing
// anything — it passed while the bug existed and fails once it is fixed, which is backwards. The
// failing bytes are therefore stated here, and the workspace supplies only the application.
const reproBuggyDockerfile = `FROM node:22-slim AS builder

WORKDIR /app

COPY package*.json ./
RUN npm install --production

COPY . .

RUN npm run build

FROM node:22-slim

WORKDIR /app

COPY --from=builder /app/dist ./dist

EXPOSE 3000

CMD ["node", "dist/server.js"]
`

func TestReproPortfolioComposeHeal(t *testing.T) {
	if _, err := os.Stat(reproSourceDir); err != nil {
		t.Skipf("repro corpus absent: %v", err)
	}

	tmp := t.TempDir()
	for _, name := range []string{"package.json", "package-lock.json", "index.html", ".dockerignore"} {
		raw, err := os.ReadFile(filepath.Join(reproSourceDir, name))
		if err != nil {
			continue
		}
		if err := os.WriteFile(filepath.Join(tmp, name), raw, 0644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(tmp, "Dockerfile"), []byte(reproBuggyDockerfile), 0644); err != nil {
		t.Fatal(err)
	}

	profile := DetectProject(tmp)
	t.Logf("PROFILE lang=%s framework=%s appType=%s buildScript=%v staticServe=%v rootPkg=%v outDir=%q start=%v",
		profile.PrimaryLanguage, profile.Framework, profile.AppType, profile.HasBuildScript,
		profile.StaticServeRequired, profile.HasRootPackageJson, profile.BuildOutputDir, profile.StartCommand)

	before := reproBuggyDockerfile

	autoHealDockerfileForCompose(tmp, SinkFunc(func(p int, s, m string) {}))

	afterRaw, _ := os.ReadFile(filepath.Join(tmp, "Dockerfile"))
	after := string(afterRaw)

	t.Logf("BEFORE:\n%s", before)
	t.Logf("AFTER:\n%s", after)

	if after == before {
		t.Errorf("healer made NO changes to a Dockerfile that cannot build (npm install --production strips tsc/vite)")
	}
	if strings.Contains(after, "--production") || strings.Contains(after, "--omit=dev") {
		t.Errorf("--production survived healing; build tools stay stripped and npm run build exits 127")
	}
	if strings.Contains(after, `"node", "dist/server.js"`) {
		t.Errorf("static Vite SPA still has a node server.js entrypoint that does not exist")
	}
	if !strings.Contains(after, "serve") {
		t.Errorf("static SPA has no static file server entrypoint")
	}
}
