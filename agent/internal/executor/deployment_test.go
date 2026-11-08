// SPDX-License-Identifier: Apache-2.0

// `deployment.apply_manifests`, and specifically the distinctions it exists to preserve.
//
// WHAT IS WORTH TESTING HERE WITHOUT A CLUSTER, and what honestly is not.
//
// A real `kubectl apply` against a real cluster belongs in the e2e stack; a fake `kubectl` on PATH would
// establish that this code can call a script, which is not a property anybody needs. What CAN be
// established here — and what every surface above this depends on — is the decision logic:
//
//   - a manifest set is confined to the workspace BEFORE any tool runs, so a signed envelope naming
//     `../../etc/passwd` is refused rather than applied;
//   - an empty set is refused instead of reported as a successful deployment of nothing;
//   - `kubectl`'s output is parsed into the workloads worth waiting for, and a Service is NOT one:
//     `rollout status` on a Service exits non-zero, so treating it as waitable would fail every manifest
//     set that contains one;
//   - the health-timeout clamp holds in both directions, and stays below the operation's own budget, or
//     "the workload did not become ready" surfaces as "the operation timed out" — a different fact with a
//     different remedy;
//   - `firstLine` returns the LAST non-empty line, because `kubectl rollout status` streams progress and
//     puts its verdict at the end. The literal first line would report "Waiting for deployment rollout to
//     finish" as the outcome of a rollout that then succeeded.
//
// The absence of a cluster is recorded in PROGRESS.md rather than hidden: this file does not claim the
// operation has been run against Kubernetes.

package executor

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

func TestParseAppliedResource(t *testing.T) {
	cases := []struct {
		name     string
		line     string
		wantKind string
		wantName string
	}{
		{"kubectl's usual shape", "deployment.apps/checkout-api created", "deployment", "checkout-api"},
		{"already unqualified", "deployment/checkout-api configured", "deployment", "checkout-api"},
		{"a service", "service/checkout-api unchanged", "service", "checkout-api"},
		{"a statefulset", "statefulset.apps/db created", "statefulset", "db"},
		{"leading and trailing space", "  daemonset.apps/node-agent created  ", "daemonset", "node-agent"},
		// An unparsable line yields an empty kind and is therefore never waited on. The safe direction:
		// the alternative is inventing a name and asking the cluster about something that is not there.
		{"a warning line", "Warning: resource is missing an annotation", "", ""},
		{"empty", "", "", ""},
		{"no slash", "deployment.apps checkout-api created", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			kind, name := parseAppliedResource(tc.line)
			if kind != tc.wantKind || name != tc.wantName {
				t.Fatalf("parseAppliedResource(%q) = (%q, %q), want (%q, %q)",
					tc.line, kind, name, tc.wantKind, tc.wantName)
			}
		})
	}
}

func TestOnlyPodBearingKindsAreWaitedOn(t *testing.T) {
	// Asserted in BOTH directions. A missing kind means a deployment reports healthy without having
	// waited for anything; an extra one means every manifest set containing a Service fails.
	for _, kind := range []string{"deployment", "statefulset", "daemonset"} {
		if _, ok := workloadKinds[kind]; !ok {
			t.Errorf("%q is not waited on, so deploying one would report healthy unverified", kind)
		}
	}
	for _, kind := range []string{"service", "configmap", "secret", "ingress", "namespace", "job"} {
		if _, ok := workloadKinds[kind]; ok {
			t.Errorf("%q is waited on, but `kubectl rollout status` has no rollout for it", kind)
		}
	}
	if len(workloadKinds) != 3 {
		t.Errorf("workloadKinds has %d entries, expected 3; update this count with the set", len(workloadKinds))
	}
}

func TestHealthTimeoutIsClampedInBothDirections(t *testing.T) {
	cases := []struct {
		name    string
		seconds int
		want    time.Duration
	}{
		{"absent falls back to the default", 0, DefaultHealthTimeout},
		{"negative falls back too", -30, DefaultHealthTimeout},
		{"an ordinary request is honoured", 90, 90 * time.Second},
		{"the ceiling is enforced", 3600, MaxHealthTimeout},
		{"exactly the ceiling", int(MaxHealthTimeout.Seconds()), MaxHealthTimeout},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := healthTimeout(tc.seconds); got != tc.want {
				t.Fatalf("healthTimeout(%d) = %s, want %s", tc.seconds, got, tc.want)
			}
		})
	}
	// The per-workload bound must stay below the operation's own budget, or a health timeout is reported
	// as an operation timeout and the reason is lost.
	if MaxHealthTimeout >= timeoutDeploy {
		t.Fatalf("MaxHealthTimeout (%s) must stay below the operation timeout (%s)",
			MaxHealthTimeout, timeoutDeploy)
	}
}

func TestFirstLineTakesTheVerdictAndNotTheProgress(t *testing.T) {
	streamed := "Waiting for deployment \"checkout-api\" rollout to finish: 0 of 3 updated replicas are available...\n" +
		"Waiting for deployment \"checkout-api\" rollout to finish: 2 of 3 updated replicas are available...\n" +
		"deployment \"checkout-api\" successfully rolled out\n"
	if got := firstLine(streamed); got != `deployment "checkout-api" successfully rolled out` {
		t.Fatalf("firstLine took %q, which is progress rather than the verdict", got)
	}
	if got := firstLine("   \n\n  \n"); got != "" {
		t.Fatalf("firstLine over whitespace = %q, want empty", got)
	}
	if got := firstLine("one line only"); got != "one line only" {
		t.Fatalf("firstLine single = %q", got)
	}
	// CRLF, because a Windows agent is a first-class host here.
	if got := firstLine("first\r\nlast\r\n"); got != "last" {
		t.Fatalf("firstLine CRLF = %q, want %q", got, "last")
	}
}

func TestResolveExactCasePath(t *testing.T) {
	tempDir := t.TempDir()
	backendDir := filepath.Join(tempDir, "Backend")
	if err := os.Mkdir(backendDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(backendDir, "package.json"), []byte("{}"), 0644); err != nil {
		t.Fatal(err)
	}
	frontentDir := filepath.Join(tempDir, "Frontent")
	if err := os.Mkdir(frontentDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(frontentDir, "package.json"), []byte("{}"), 0644); err != nil {
		t.Fatal(err)
	}

	exactPath, exists := resolveExactCasePath(tempDir, "backend/package.json")
	if !exists || exactPath != "Backend/package.json" {
		t.Fatalf("expected ('Backend/package.json', true), got (%q, %v)", exactPath, exists)
	}

	exactFrontent, existsFrontent := resolveExactCasePath(tempDir, "frontent/package.json")
	if !existsFrontent || exactFrontent != "Frontent/package.json" {
		t.Fatalf("expected ('Frontent/package.json', true), got (%q, %v)", exactFrontent, existsFrontent)
	}

	_, existsMissing := resolveExactCasePath(tempDir, "nonexistent/file.txt")
	if existsMissing {
		t.Fatalf("expected exists=false for nonexistent path")
	}
}

func TestAutoHealDockerfileForCompose_MonorepoExactCase(t *testing.T) {
	tempDir := t.TempDir()
	backendDir := filepath.Join(tempDir, "Backend")
	if err := os.Mkdir(backendDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(backendDir, "package.json"), []byte("{}"), 0644); err != nil {
		t.Fatal(err)
	}
	frontentDir := filepath.Join(tempDir, "Frontent")
	if err := os.Mkdir(frontentDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(frontentDir, "package.json"), []byte("{}"), 0644); err != nil {
		t.Fatal(err)
	}

	dockerfile := `FROM node:18-alpine
WORKDIR /app
COPY backend/package.json backend/package-lock.json* ./backend/
COPY frontent/package.json frontent/package-lock.json* ./frontent/
RUN cd backend && npm install
RUN cd frontent && npm install
COPY . .
RUN cd frontent && npm run build
EXPOSE 5000 3000
CMD ["npm", "start"]
`
	if err := os.WriteFile(filepath.Join(tempDir, "Dockerfile"), []byte(dockerfile), 0644); err != nil {
		t.Fatal(err)
	}

	sink := SinkFunc(func(percent int, stage, message string) {})
	autoHealDockerfileForCompose(tempDir, sink)

	healedBytes, err := os.ReadFile(filepath.Join(tempDir, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	healed := string(healedBytes)

	if !strings.Contains(healed, "COPY Backend/package.json ./Backend") {
		t.Fatalf("expected Backend/package.json healed, got:\n%s", healed)
	}
	if !strings.Contains(healed, "COPY Frontent/package.json ./Frontent") {
		t.Fatalf("expected Frontent/package.json healed, got:\n%s", healed)
	}
	if !strings.Contains(healed, "RUN cd Backend && npm install") {
		t.Fatalf("expected cd Backend healed, got:\n%s", healed)
	}
	if !strings.Contains(healed, "RUN cd Frontent && npm run build") {
		t.Fatalf("expected cd Frontent healed, got:\n%s", healed)
	}
}

func TestAutoHealDockerfileForCompose_NoRootPackageJson(t *testing.T) {
	tempDir := t.TempDir()
	backendDir := filepath.Join(tempDir, "Backend")
	if err := os.Mkdir(backendDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(backendDir, "package.json"), []byte("{}"), 0644); err != nil {
		t.Fatal(err)
	}
	frontentDir := filepath.Join(tempDir, "Frontent")
	if err := os.Mkdir(frontentDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(frontentDir, "package.json"), []byte("{}"), 0644); err != nil {
		t.Fatal(err)
	}

	dockerfile := `FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["node", "Backend/server.js"]
`
	if err := os.WriteFile(filepath.Join(tempDir, "Dockerfile"), []byte(dockerfile), 0644); err != nil {
		t.Fatal(err)
	}

	sink := SinkFunc(func(percent int, stage, message string) {})
	autoHealDockerfileForCompose(tempDir, sink)

	healedBytes, err := os.ReadFile(filepath.Join(tempDir, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	healed := string(healedBytes)

	if !strings.Contains(healed, "skipped root package copy") {
		t.Fatalf("expected root package copy skipped, got:\n%s", healed)
	}
	if !strings.Contains(healed, "(cd Backend && npm install)") || !strings.Contains(healed, "(cd Frontent && npm install)") {
		t.Fatalf("expected subfolder installs, got:\n%s", healed)
	}
	if !strings.Contains(healed, "(cd Frontent && npm run build)") {
		t.Fatalf("expected subfolder build, got:\n%s", healed)
	}
}

func TestAutoHealDockerfileForCompose_FrontendMonorepo(t *testing.T) {
	tempDir := t.TempDir()
	frontentDir := filepath.Join(tempDir, "Frontent")
	if err := os.Mkdir(frontentDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(frontentDir, "package.json"), []byte(`{"name":"frontent","scripts":{"build":"vite build"}}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(frontentDir, "vite.config.js"), []byte(`export default {}`), 0644); err != nil {
		t.Fatal(err)
	}

	composeFile := `services:
  frontend:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000"]
      interval: 10s
`
	if err := os.WriteFile(filepath.Join(tempDir, "docker-compose.yml"), []byte(composeFile), 0644); err != nil {
		t.Fatal(err)
	}

	dockerfile := `FROM node:20-alpine
WORKDIR /app
COPY Frontent/package.json ./Frontent/package.json
RUN (cd Frontent && npm install)
COPY . .
RUN (cd Frontent && npm run build) || true
EXPOSE 3000
HEALTHCHECK CMD curl -f http://localhost:3000/health || exit 1
USER 1001
CMD ["node", "Backend/server.js"]
`
	if err := os.WriteFile(filepath.Join(tempDir, "Dockerfile"), []byte(dockerfile), 0644); err != nil {
		t.Fatal(err)
	}

	sink := SinkFunc(func(percent int, stage, message string) {})
	autoHealDockerfileForCompose(tempDir, sink)

	healedDBytes, err := os.ReadFile(filepath.Join(tempDir, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	healedD := string(healedDBytes)

	if !strings.Contains(healedD, "npm install -g serve") {
		t.Fatalf("expected serve installed in Dockerfile, got:\n%s", healedD)
	}
	// A full-stack app's own server is kept. This used to be rewritten to a static `serve` of the frontend
	// because one project's folder was named "Frontent" -- which threw the backend API away.
	if !strings.Contains(healedD, `CMD ["node", "Backend/server.js"]`) {
		t.Fatalf("expected the backend server command to be kept, got:\n%s", healedD)
	}
	// No credentials are ever invented: the healer used to inject one app's OAuth ids, secrets and a
	// MONGO_URI into every project's image.
	for _, invented := range []string{"GOOGLE_CLIENT_ID", "SESSION_SECRET", "JWT_SECRET", "MONGO_URI"} {
		if strings.Contains(healedD, invented) {
			t.Fatalf("healer invented %s in the Dockerfile:\n%s", invented, healedD)
		}
	}

	healedCBytes, err := os.ReadFile(filepath.Join(tempDir, "docker-compose.yml"))
	if err != nil {
		t.Fatal(err)
	}
	healedC := string(healedCBytes)

	if strings.Contains(healedC, "curl") {
		t.Fatalf("expected curl healthcheck removed from docker-compose.yml, got:\n%s", healedC)
	}
	if !strings.Contains(healedC, "wget") {
		t.Fatalf("expected wget spider healthcheck in docker-compose.yml, got:\n%s", healedC)
	}
	if strings.Contains(healedD, `\ CMD`) {
		t.Fatalf("expected backslash before CMD removed, got:\n%s", healedD)
	}
}

func TestAutoHealDockerfileForCompose_HealthcheckWithBackslash(t *testing.T) {
	tempDir := t.TempDir()
	dockerfile := `FROM node:20-alpine
WORKDIR /app
COPY . .
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \ CMD wget -q --spider http://127.0.0.1:3000/ || exit 0
CMD ["node", "server.js"]
`
	if err := os.WriteFile(filepath.Join(tempDir, "Dockerfile"), []byte(dockerfile), 0644); err != nil {
		t.Fatal(err)
	}

	sink := SinkFunc(func(percent int, stage, message string) {})
	autoHealDockerfileForCompose(tempDir, sink)

	healedDBytes, err := os.ReadFile(filepath.Join(tempDir, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	healedD := string(healedDBytes)

	if strings.Contains(healedD, `\ CMD`) {
		t.Fatalf("expected backslash before CMD removed from HEALTHCHECK, got:\n%s", healedD)
	}
	// The flags the author set survive, and the probe is one that CAN RUN IN THIS IMAGE.
	//
	// This used to assert `wget -q --spider ... || exit 0`. That command was wrong twice over:
	// `wget` is absent from `node:*-slim` (the base image of every Node project this platform
	// generates), so the probe never tested anything; and `|| exit 0` made it exit successfully
	// anyway, so the container reported HEALTHY with a completely dead listener. A real deployment
	// showed this in its health log — `"/bin/sh: 1: wget: not found"` with `ExitCode: 0`.
	if !strings.Contains(healedD, "HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3") {
		t.Fatalf("expected the HEALTHCHECK's own flags to survive, got:\n%s", healedD)
	}
	// The language is inferred from `FROM node:20-alpine`, so the probe is Node's own fetch.
	if !strings.Contains(healedD, `node -e "fetch(`) {
		t.Fatalf("expected a probe that can run in a node image, got:\n%s", healedD)
	}
	if strings.Contains(healedD, "wget") {
		t.Fatalf("wget does not exist in node:*-slim, so this probe can never test anything, got:\n%s", healedD)
	}
	if regexp.MustCompile(`(?m)^HEALTHCHECK.*\|\| exit 0`).MatchString(healedD) {
		t.Fatalf("a healthcheck that cannot fail is not a healthcheck, got:\n%s", healedD)
	}
}

func TestHealerNeverAddsNpmToANonNodeImage(t *testing.T) {
	tempDir := t.TempDir()
	// A frontend marker file is present, which is what used to trigger the npm line regardless of base image.
	if err := os.WriteFile(filepath.Join(tempDir, "index.html"), []byte("<html></html>"), 0o644); err != nil {
		t.Fatal(err)
	}
	dockerfile := "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nUSER 1001\nCMD [\"python\", \"app.py\"]\n"
	if err := os.WriteFile(filepath.Join(tempDir, "Dockerfile"), []byte(dockerfile), 0o644); err != nil {
		t.Fatal(err)
	}
	autoHealDockerfileForCompose(tempDir, SinkFunc(func(int, string, string) {}))
	healed, err := os.ReadFile(filepath.Join(tempDir, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(healed), "npm install") {
		t.Fatalf("npm added to a python image:\n%s", healed)
	}
}

func TestFinalStageIsNode(t *testing.T) {
	cases := map[string]bool{
		"FROM node:22-alpine AS build\nFROM nginx:1.27-alpine\n":  false,
		"FROM python:3.12 AS deps\nFROM node:20-slim\n":           true,
		"FROM --platform=linux/amd64 docker.io/library/node:20\n": true,
		"ARG X=1\n": false,
	}
	for dockerfile, want := range cases {
		if got := finalStageIsNode(dockerfile); got != want {
			t.Errorf("finalStageIsNode(%q) = %v, want %v", dockerfile, got, want)
		}
	}
}

func TestBuildFilesAreRestoredAfterHealing(t *testing.T) {
	dir := t.TempDir()
	original := "FROM node:22-alpine\nUSER node\nCMD [\"node\", \"server.js\"]\n"
	if err := os.WriteFile(filepath.Join(dir, "Dockerfile"), []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	snap := snapshotBuildFiles(dir)
	// What a heal does: rewrite one file and create another.
	if err := os.WriteFile(filepath.Join(dir, "Dockerfile"), []byte("FROM node:22-alpine\n# healed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "docker-compose.yml"), []byte("services: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	restored := snap.restore()
	got, err := os.ReadFile(filepath.Join(dir, "Dockerfile"))
	if err != nil || string(got) != original {
		t.Fatalf("Dockerfile not restored byte for byte: %q (%v)", got, err)
	}
	if _, err := os.Stat(filepath.Join(dir, "docker-compose.yml")); !os.IsNotExist(err) {
		t.Fatalf("a file the healer created was left behind")
	}
	if len(restored) != 2 {
		t.Fatalf("expected two restored files, got %v", restored)
	}
	if again := snap.restore(); len(again) != 0 {
		t.Fatalf("restoring an unchanged workspace changed %v", again)
	}
}

func TestHealthProbeMatchesTheFinalImage(t *testing.T) {
	cases := map[string]string{
		"FROM node:22 AS b\nFROM nginxinc/nginx-unprivileged:1.27.3-alpine\n": "wget -q",
		"FROM node:22 AS b\nFROM nginx:1.27\n":                                "curl -fsS",
		"FROM node:22-slim\n":                                                 "node -e",
		"FROM python:3.12-slim\n":                                             "python -c",
		"FROM gcr.io/distroless/static\n":                                     "",
	}
	for dockerfile, want := range cases {
		got := probeForFinalImage(dockerfile, 3000)
		if (want == "" && got != "") || (want != "" && !strings.Contains(got, want)) {
			t.Errorf("probeForFinalImage(%q) = %q, want it to contain %q", dockerfile, got, want)
		}
	}
}

func TestHealerKeepsAWorkingProbeForAStaticSite(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "package.json"), []byte(`{"scripts":{"build":"vite build"}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	dockerfile := "FROM node:22 AS builder\nWORKDIR /app\nCOPY . .\nRUN npm ci && npm run build\n" +
		"FROM nginxinc/nginx-unprivileged:1.27.3-alpine\nCOPY --from=builder /app/dist /usr/share/nginx/html\n" +
		"HEALTHCHECK --interval=30s CMD wget -q -O /dev/null http://127.0.0.1:3000/ || exit 1\nUSER 101\n" +
		"CMD [\"nginx\", \"-g\", \"daemon off;\"]\n"
	if err := os.WriteFile(filepath.Join(dir, "Dockerfile"), []byte(dockerfile), 0o644); err != nil {
		t.Fatal(err)
	}
	autoHealDockerfileForCompose(dir, SinkFunc(func(int, string, string) {}))
	healed, _ := os.ReadFile(filepath.Join(dir, "Dockerfile"))
	if strings.Contains(string(healed), "node -e") {
		t.Fatalf("a node probe was put into an nginx image:\n%s", healed)
	}
}

func TestATakenHostPortIsMovedToAFreeOne(t *testing.T) {
	busy, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	defer busy.Close()
	port := busy.Addr().(*net.TCPAddr).Port
	dir := t.TempDir()
	compose := filepath.Join(dir, "docker-compose.yml")
	body := fmt.Sprintf("services:\n  web:\n    image: nginx\n    ports:\n      - \"%d:3000\"\n", port)
	if err := os.WriteFile(compose, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	freeTakenHostPorts(context.Background(), &validator.Runner{Dir: dir}, compose, "forgeops-test-none", SinkFunc(func(int, string, string) {}))
	got, _ := os.ReadFile(compose)
	if strings.Contains(string(got), fmt.Sprintf("\"%d:3000\"", port)) || !strings.Contains(string(got), ":3000\"") {
		t.Fatalf("the taken host port %d was not moved:\n%s", port, got)
	}
}
