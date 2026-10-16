// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// deploymentArchetype is one application shape ForgeOps claims to support. The matrix below is the
// regression net for the platform's central promise: any valid application, in any language, with or
// without a Dockerfile, deploys. A new archetype a user hits in the field becomes a row here.
type deploymentArchetype struct {
	name string

	// files laid down in the repository, relative path to content.
	files map[string]string

	// dockerfile the platform would ship for this repository. Empty means the repository
	// has none, so the platform must produce one itself.
	dockerfile string

	// compose the repository already carries, if any.
	compose string

	// expectBuild is false for archetypes whose real image build is too heavy for a unit run
	// (Maven, Next.js); they are still healed and statically checked.
	expectBuild bool

	// validation runs against the healed Dockerfile regardless of whether a build happened.
	validation func(t *testing.T, healed, baseDir string)
}

const nodeApiPackageJSON = `{"name":"api","main":"server.js","scripts":{"start":"node server.js"},"dependencies":{"express":"^4.18.2"}}`

const vitePackageJSON = `{"name":"spa","private":true,"type":"module","scripts":{"build":"tsc -b && vite build"},"dependencies":{"react":"^19.0.0","react-dom":"^19.0.0"},"devDependencies":{"@types/node":"^22.10.0","@types/react":"^19.0.0","@types/react-dom":"^19.0.0","@vitejs/plugin-react":"^4.3.4","typescript":"~5.7.2","vite":"^6.1.0"}}`

// viteProjectFiles is a minimal but COMPLETE Vite + TypeScript project. The completeness matters: a
// `tsc -b && vite build` script needs a tsconfig graph, an entry module, and a mounted root, so a
// fixture missing any of them fails for reasons that have nothing to do with the platform.
func viteProjectFiles() map[string]string {
	return map[string]string{
		"package.json": vitePackageJSON,
		"index.html": `<!doctype html>
<html lang="en">
  <head><meta charset="UTF-8" /><title>spa</title></head>
  <body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body>
</html>`,
		"vite.config.ts": `import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({ plugins: [react()] });
`,
		"tsconfig.json": `{
  "files": [],
  "references": [{ "path": "./tsconfig.app.json" }, { "path": "./tsconfig.node.json" }]
}
`,
		"tsconfig.app.json": `{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": []
  },
  "include": ["src"]
}
`,
		"tsconfig.node.json": `{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2023"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["vite.config.ts"]
}
`,
		"src/main.tsx": `import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <h1>spa</h1>
  </StrictMode>,
);
`,
	}
}

// archetypes is the full support matrix. Each entry is a shape a user can plausibly hand ForgeOps.
func archetypes() []deploymentArchetype {
	return []deploymentArchetype{
		{
			name:  "vite-spa",
			files: viteProjectFiles(),
			// The exact shape that produced `npm run build` exit code 127 in the field:
			// --production strips tsc/vite, and the CMD names a server.js that a static SPA never has.
			dockerfile: `FROM node:22-slim AS builder
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
`,
			compose:     "services:\n  web:\n    build: .\n    ports:\n      - \"3000:3000\"\n",
			expectBuild: true,
			validation: func(t *testing.T, healed, _ string) {
				requireAbsent(t, healed, "--production", "--omit=dev")
				requireAbsent(t, healed, `"node", "dist/server.js"`)
				requirePresent(t, healed, "serve")
			},
		},
		{
			name: "node-api-no-build-step",
			files: map[string]string{
				"package.json": nodeApiPackageJSON,
				"server.js":    "const e=require('express')();e.get('/',(q,s)=>s.send('ok'));e.listen(3000)",
			},
			// Hallucinated build step for a project with no build script: must be neutralised,
			// not run, or the build dies on a missing script.
			dockerfile: `FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000
CMD ["node", "server.js"]
`,
			expectBuild: true,
			validation: func(t *testing.T, healed, _ string) {
				if strings.Contains(healed, "RUN npm run build") && !strings.Contains(healed, "auto-healed") {
					t.Errorf("no-build-script project still runs `npm run build`, which exits non-zero")
				}
				requirePresent(t, healed, "server.js")
			},
		},
		{
			name: "python-fastapi",
			files: map[string]string{
				"requirements.txt": "fastapi==0.109.0\nuvicorn==0.27.0\n",
				"main.py":          "from fastapi import FastAPI\napp = FastAPI()\n",
			},
			dockerfile: `FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
`,
			expectBuild: true,
		},
		{
			name: "go-service",
			files: map[string]string{
				"go.mod":  "module example.com/svc\n\ngo 1.23\n",
				"main.go": "package main\nimport \"net/http\"\nfunc main(){http.HandleFunc(\"/\",func(w http.ResponseWriter,r *http.Request){w.Write([]byte(\"ok\"))});http.ListenAndServe(\":8080\",nil)}\n",
			},
			dockerfile: `FROM golang:1.23-alpine AS builder
WORKDIR /app
COPY go.mod ./
COPY . .
RUN CGO_ENABLED=0 go build -o /app/server .

FROM alpine:3.20
WORKDIR /app
COPY --from=builder /app/server /app/server
EXPOSE 8080
CMD ["/app/server"]
`,
			expectBuild: true,
		},
		{
			name: "static-html",
			files: map[string]string{
				"index.html": "<!doctype html><html><body><h1>hi</h1></body></html>",
			},
			dockerfile: `FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE 80
`,
			expectBuild: false,
			validation: func(t *testing.T, healed, _ string) {
				requirePresent(t, healed, "nginx")
			},
		},
		{
			name: "monorepo-frontend-backend",
			files: map[string]string{
				"backend/package.json":  nodeApiPackageJSON,
				"backend/server.js":     "require('http').createServer((q,s)=>s.end('ok')).listen(5000)",
				"Frontent/package.json": vitePackageJSON,
				"Frontent/index.html":   "<!doctype html><html><body></body></html>",
			},
			// Root has no package.json: the platform must route to the subprojects, and
			// `Frontent` is the misspelling the repository actually uses on disk.
			dockerfile: `FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
EXPOSE 3000 5000
CMD ["node", "Backend/server.js"]
`,
			expectBuild: false,
			validation: func(t *testing.T, healed, _ string) {
				requireNoActiveInstruction(t, healed, "COPY package*.json ./")
			},
		},
		{
			name: "app-in-subdirectory",
			files: map[string]string{
				"apps/web/package.json": vitePackageJSON,
				"apps/web/index.html":   "<!doctype html><html><body></body></html>",
				"README.md":             "# repo with the real app nested under apps/web",
			},
			// Build context is the subdirectory, so the Dockerfile is written from there.
			dockerfile: `FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
`,
			expectBuild: false,
		},
		{
			name: "java-maven",
			files: map[string]string{
				"pom.xml": "<project><modelVersion>4.0.0</modelVersion><groupId>g</groupId><artifactId>a</artifactId><version>1</version></project>",
			},
			dockerfile: `FROM maven:3.9-eclipse-temurin-21 AS builder
WORKDIR /app
COPY pom.xml .
COPY . .
RUN mvn clean package -DskipTests

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=builder /app/target/*.jar /app/app.jar
EXPOSE 8080
CMD ["java", "-jar", "/app/app.jar"]
`,
			expectBuild: false,
			validation: func(t *testing.T, healed, _ string) {
				requirePresent(t, healed, "mvn")
			},
		},
		{
			name: "next-js",
			files: map[string]string{
				"package.json": `{"name":"web","scripts":{"build":"next build","start":"next start"},"dependencies":{"next":"^14.2.0","react":"^18.3.1"}}`,
			},
			dockerfile: `FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
EXPOSE 3000
CMD ["npm", "start"]
`,
			expectBuild: false,
			validation: func(t *testing.T, healed, _ string) {
				requireAbsent(t, healed, "--production", "--omit=dev")
				requirePresent(t, healed, "npm")
			},
		},
		{
			name: "no-dockerfile-anywhere",
			files: func() map[string]string {
				files := viteProjectFiles()
				// The compose stack the platform synthesises for a generated Dockerfile.
				files["docker-compose.yml"] = "services:\n  app:\n    build:\n      context: .\n    ports:\n      - \"3000:3000\"\n"
				return files
			}(),
			// No Dockerfile anywhere: the platform must generate a buildable one from the
			// detected profile. This is the case that leaves "nothing was deployed".
			dockerfile:  "",
			expectBuild: true,
			validation: func(t *testing.T, healed, _ string) {
				if healed == "" {
					t.Fatalf("no Dockerfile was generated for a repository that has none")
				}
				requirePresent(t, healed, "FROM")
			},
		},
	}
}

func requirePresent(t *testing.T, haystack string, needles ...string) {
	t.Helper()
	for _, n := range needles {
		if !strings.Contains(haystack, n) {
			t.Errorf("expected %q in healed Dockerfile, got:\n%s", n, haystack)
		}
	}
}

func requireAbsent(t *testing.T, haystack string, needles ...string) {
	t.Helper()
	for _, n := range needles {
		if strings.Contains(haystack, n) {
			t.Errorf("did not expect %q in healed Dockerfile, got:\n%s", n, haystack)
		}
	}
}

// requireNoActiveInstruction fails when an instruction survives as live Dockerfile syntax. The healer
// neutralises a harmful line by commenting it out, so a plain substring search would report the fix
// as a failure — and, worse, would accept a commented-out line as if it still ran.
func requireNoActiveInstruction(t *testing.T, dockerfile, prefix string) {
	t.Helper()
	for _, raw := range strings.Split(strings.ReplaceAll(dockerfile, "\r\n", "\n"), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(strings.ToUpper(line), strings.ToUpper(prefix)) {
			t.Errorf("active %q instruction survived healing:\n%s", prefix, dockerfile)
			return
		}
	}
}

// TestDeploymentArchetypeMatrix heals the Dockerfile for every supported application shape and
// statically asserts the result is buildable. It is the guard against one-off fixes: a change that
// makes the portfolio deploy while breaking Go or Python fails here.
func TestDeploymentArchetypeMatrix(t *testing.T) {
	for _, arch := range archetypes() {
		t.Run(arch.name, func(t *testing.T) {
			baseDir := t.TempDir()
			writeArchetype(t, baseDir, arch)

			sink := SinkFunc(func(percent int, stage, message string) {})

			// The platform's own entry point: heal whatever is there, generate what is missing.
			ensureBuildableArtifacts(baseDir, sink)

			healed := readFileOrEmpty(filepath.Join(baseDir, "Dockerfile"))

			if arch.validation != nil {
				arch.validation(t, healed, baseDir)
			}

			// A Dockerfile the platform produces must at minimum be a real Dockerfile.
			if !strings.Contains(healed, "FROM") {
				t.Fatalf("no FROM instruction after healing; nothing can be built:\n%s", healed)
			}
			if strings.Contains(healed, "frontent") {
				t.Errorf("hallucinated directory 'frontent' survived healing:\n%s", healed)
			}
		})
	}
}

// TestDeploymentArchetypeMatrixDockerBuild is the matrix again, with a real `docker compose build`
// per archetype. Opt in with FORGEOPS_DOCKER_E2E=1: it pulls base images and needs a working daemon,
// so it is not part of the default unit run.
func TestDeploymentArchetypeMatrixDockerBuild(t *testing.T) {
	if os.Getenv("FORGEOPS_DOCKER_E2E") != "1" {
		t.Skip("set FORGEOPS_DOCKER_E2E=1 to run real container builds")
	}
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("docker not on PATH")
	}

	for _, arch := range archetypes() {
		if !arch.expectBuild {
			continue
		}
		t.Run(arch.name, func(t *testing.T) {
			baseDir := t.TempDir()
			writeArchetype(t, baseDir, arch)

			sink := SinkFunc(func(percent int, stage, message string) {})
			ensureBuildableArtifacts(baseDir, sink)

			cmd := exec.Command("docker", "compose", "-p", "forgeops-e2e-"+arch.name, "build")
			cmd.Dir = baseDir
			out, err := cmd.CombinedOutput()
			if err != nil {
				message := string(out)
				// A registry handshake timing out is the network, not the deployment system. Retrying
				// it here would hide real failures behind a flaky pass, so it is reported as skipped.
				if strings.Contains(message, "TLS handshake timeout") || strings.Contains(message, "failed to do request") {
					t.Skipf("registry unreachable for %s, cannot attribute the failure to the platform:\n%s",
						arch.name, tail(message, 12))
				}
				t.Fatalf("docker compose build failed for archetype %s:\n%s", arch.name, tail(message, 60))
			}
			t.Cleanup(func() {
				cleanup := exec.Command("docker", "compose", "-p", "forgeops-e2e-"+arch.name, "down", "--rmi", "local", "-v")
				cleanup.Dir = baseDir
				_ = cleanup.Run()
			})
		})
	}
}

func writeArchetype(t *testing.T, baseDir string, arch deploymentArchetype) {
	t.Helper()
	for rel, content := range arch.files {
		full := filepath.Join(baseDir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0644); err != nil {
			t.Fatal(err)
		}
	}
	if arch.dockerfile != "" {
		if err := os.WriteFile(filepath.Join(baseDir, "Dockerfile"), []byte(arch.dockerfile), 0644); err != nil {
			t.Fatal(err)
		}
	}
	if arch.compose != "" {
		if err := os.WriteFile(filepath.Join(baseDir, "docker-compose.yml"), []byte(arch.compose), 0644); err != nil {
			t.Fatal(err)
		}
	}
}

func readFileOrEmpty(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(raw)
}

func tail(text string, lines int) string {
	split := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	if len(split) <= lines {
		return text
	}
	return fmt.Sprintf("... (%d earlier lines)\n%s", len(split)-lines, strings.Join(split[len(split)-lines:], "\n"))
}
