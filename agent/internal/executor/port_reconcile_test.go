// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// THE DEFECT THESE TESTS EXIST FOR, reproduced from the deployment that found it.
//
// A model generated a Dockerfile with `EXPOSE 8080` and `CMD ["npm", "start"]`. The healer correctly
// recognised a static SPA and rewrote the CMD to `serve -s dist -l tcp://0.0.0.0:3000` and the
// HEALTHCHECK to probe 3000 — and left `EXPOSE 8080`, because nothing had ever owned that line. The
// compose port was then picked FROM `EXPOSE`, so `8080:8080` was published against a process bound to
// 3000: Docker accepted the connection and closed it, which is `ERR_EMPTY_RESPONSE`.
//
// The deployment reported `applied` / `healthy` / `ready`, and every word was true. The healthcheck
// runs inside the container and probed 3000, so it passed. Only the PUBLISHED port was wrong, and
// nothing compared "the port we published" against "the port the process binds".
func TestHealerLeavesNoPortContradiction(t *testing.T) {
	dir := t.TempDir()

	// Byte-for-byte the shape that failed: EXPOSE disagreeing with CMD, ENV and HEALTHCHECK.
	dockerfile := `FROM node:22-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/dist ./dist
EXPOSE 8080
RUN npm install -g serve
ENV PORT=3000 NODE_ENV=production
USER 10001
HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD node -e "fetch('http://127.0.0.1:3000/')"
CMD ["serve", "-s", "dist", "-l", "tcp://0.0.0.0:3000"]
`
	compose := `services:
    web:
        build:
            context: .
            dockerfile: Dockerfile
        environment:
            - PORT=3000
        ports:
            - 8080:8080
        restart: unless-stopped
`
	write(t, dir, "Dockerfile", dockerfile)
	write(t, dir, "docker-compose.yml", compose)
	write(t, dir, "package.json", `{"name":"spa","scripts":{"build":"vite build"},"devDependencies":{"vite":"^6.1.0"}}`)
	write(t, dir, "index.html", "<!doctype html><html><body></body></html>")

	autoHealDockerfileForCompose(dir, SinkFunc(func(int, string, string) {}))

	healedDockerfile := readFileOrEmpty(filepath.Join(dir, "Dockerfile"))
	healedCompose := readFileOrEmpty(filepath.Join(dir, "docker-compose.yml"))

	// 1. EXPOSE now names the port the process binds, not the one the model guessed.
	if !strings.Contains(healedDockerfile, "EXPOSE 3000") {
		t.Errorf("EXPOSE was not reconciled to the bound port:\n%s", healedDockerfile)
	}
	if strings.Contains(healedDockerfile, "EXPOSE 8080") {
		t.Errorf("the contradicting EXPOSE survived healing:\n%s", healedDockerfile)
	}

	// 2. The compose mapping forwards to the port the process binds.
	if !strings.Contains(healedCompose, "8080:3000") {
		t.Errorf("the compose container port still disagrees with the listener:\n%s", healedCompose)
	}

	// 3. THE HOST PORT IS PRESERVED. Fixing the connection by moving the URL would break the link the
	//    UI prints and anything a person has bookmarked.
	if !strings.Contains(healedCompose, "8080:") {
		t.Errorf("the host port was changed; only the container side was ever wrong:\n%s", healedCompose)
	}
}

// The reconciliation must be a no-op on a file that is already consistent: a healer that rewrites
// correct input is a healer nobody can predict.
func TestReconcileLeavesConsistentFilesAlone(t *testing.T) {
	dockerfile := "FROM node:22-slim\nEXPOSE 3000\nCMD [\"node\", \"server.js\"]\n"
	if got := reconcileDockerfilePort(dockerfile, 3000); got != dockerfile {
		t.Errorf("a consistent Dockerfile was rewritten:\nwant:\n%s\ngot:\n%s", dockerfile, got)
	}

	compose := "services:\n  web:\n    ports:\n      - 8080:3000\n"
	if got := reconcileComposePort(compose, 3000); got != compose {
		t.Errorf("a consistent compose file was rewritten:\nwant:\n%s\ngot:\n%s", compose, got)
	}
}

func TestReconcileDockerfilePortHandlesTheShapesThatOccur(t *testing.T) {
	cases := []struct {
		name string
		in   string
		port int
		want string
		deny string
	}{
		{
			name: "a multi-port EXPOSE collapses to the bound one",
			in:   "FROM x:1\nEXPOSE 8080 9090\nCMD [\"a\"]\n",
			port: 3000,
			want: "EXPOSE 3000",
			deny: "9090",
		},
		{
			name: "indentation is preserved",
			in:   "FROM x:1\n  EXPOSE 8080\nCMD [\"a\"]\n",
			port: 3000,
			want: "  EXPOSE 3000",
		},
		{
			name: "a missing EXPOSE is added",
			in:   "FROM x:1\nWORKDIR /app\nCMD [\"a\"]\n",
			port: 4000,
			want: "EXPOSE 4000",
		},
		{
			name: "lowercase expose is still a declaration",
			in:   "FROM x:1\nexpose 8080\nCMD [\"a\"]\n",
			port: 3000,
			want: "EXPOSE 3000",
			deny: "8080",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := reconcileDockerfilePort(tc.in, tc.port)
			if !strings.Contains(got, tc.want) {
				t.Errorf("want %q in:\n%s", tc.want, got)
			}
			if tc.deny != "" && strings.Contains(got, tc.deny) {
				t.Errorf("did not want %q in:\n%s", tc.deny, got)
			}
		})
	}
}

func TestReconcileComposePortHandlesTheShapesThatOccur(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{name: "bare", in: "    ports:\n      - 8080:8080\n", want: "- 8080:3000"},
		{name: "quoted", in: "    ports:\n      - \"8080:8080\"\n", want: "- \"8080:3000\""},
		{name: "bound to an interface", in: "    ports:\n      - 127.0.0.1:8080:8080\n", want: "127.0.0.1:8080:3000"},
		{name: "with a protocol", in: "    ports:\n      - \"8080:8080/tcp\"\n", want: "8080:3000/tcp"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := reconcileComposePort(tc.in, 3000)
			if !strings.Contains(got, tc.want) {
				t.Errorf("want %q in:\n%s", tc.want, got)
			}
		})
	}
}

// A port the reconciler cannot establish must leave both files untouched, rather than rewriting them
// towards a guess.
func TestReconcileRefusesWithoutAPort(t *testing.T) {
	dockerfile := "FROM x:1\nEXPOSE 8080\n"
	if got := reconcileDockerfilePort(dockerfile, 0); got != dockerfile {
		t.Errorf("a zero port rewrote the Dockerfile anyway:\n%s", got)
	}
	compose := "    ports:\n      - 8080:8080\n"
	if got := reconcileComposePort(compose, 0); got != compose {
		t.Errorf("a zero port rewrote the compose file anyway:\n%s", got)
	}
}

func TestPortFromEnvTakesLastDeclaration(t *testing.T) {
	dockerfile := `FROM node:22-slim
WORKDIR /app
ENV PORT=8080
ENV PORT=3000 NODE_ENV=production
CMD ["npm", "start"]
`
	if got := portFromEnv(dockerfile); got != 3000 {
		t.Fatalf("portFromEnv = %d, want 3000 (last ENV declaration must win)", got)
	}
}

func TestPortFromComposeEnv(t *testing.T) {
	cases := []struct {
		name    string
		compose string
		want    int
	}{
		{"mapping style", "services:\n  web:\n    environment:\n      PORT: 3000\n", 3000},
		{"mapping with fallback", "services:\n  web:\n    environment:\n      PORT: \"${PORT:-3000}\"\n", 3000},
		{"list style", "services:\n  web:\n    environment:\n      - PORT=8080\n", 8080},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := portFromComposeEnv(tc.compose); got != tc.want {
				t.Fatalf("portFromComposeEnv = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestReconcileDockerfilePortReconcilesEnvPort(t *testing.T) {
	dockerfile := `FROM node:22-slim
ENV PORT=8080
EXPOSE 8080
CMD ["npm", "start"]
`
	got := reconcileDockerfilePort(dockerfile, 3000)
	if !strings.Contains(got, "ENV PORT=3000") {
		t.Errorf("ENV PORT was not reconciled to 3000:\n%s", got)
	}
	if strings.Contains(got, "ENV PORT=8080") {
		t.Errorf("stale ENV PORT=8080 survived:\n%s", got)
	}
	if !strings.Contains(got, "EXPOSE 3000") {
		t.Errorf("EXPOSE was not reconciled to 3000:\n%s", got)
	}
}

func write(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
}
