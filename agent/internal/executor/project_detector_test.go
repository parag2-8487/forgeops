// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProjectDetector_ViteSPA(t *testing.T) {
	tempDir := t.TempDir()
	pkgJson := `{
		"name": "my-portfolio",
		"private": true,
		"version": "0.0.0",
		"type": "module",
		"scripts": {
			"dev": "vite",
			"build": "tsc -b && vite build",
			"preview": "vite preview"
		},
		"dependencies": {
			"lucide-react": "^0.475.0",
			"react": "^19.0.0",
			"react-dom": "^19.0.0"
		},
		"devDependencies": {
			"@tailwindcss/vite": "^4.0.6",
			"@types/react": "^19.0.8",
			"@types/react-dom": "^19.0.3",
			"@vitejs/plugin-react": "^4.3.4",
			"tailwindcss": "^4.0.6",
			"typescript": "~5.7.2",
			"vite": "^6.1.0"
		}
	}`
	if err := os.WriteFile(filepath.Join(tempDir, "package.json"), []byte(pkgJson), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tempDir, "index.html"), []byte("<!doctype html><html></html>"), 0644); err != nil {
		t.Fatal(err)
	}

	profile := DetectProject(tempDir)
	if profile.PrimaryLanguage != LangNode {
		t.Fatalf("expected LangNode, got %v", profile.PrimaryLanguage)
	}
	if profile.AppType != AppTypeStaticSPA {
		t.Fatalf("expected AppTypeStaticSPA, got %v", profile.AppType)
	}
	if !profile.HasBuildScript {
		t.Fatalf("expected HasBuildScript true")
	}
	if !profile.StaticServeRequired {
		t.Fatalf("expected StaticServeRequired true")
	}
	if profile.BuildOutputDir != "dist" {
		t.Fatalf("expected BuildOutputDir dist, got %s", profile.BuildOutputDir)
	}

	// Test healing of buggy Dockerfile with --production and node dist/server.js
	buggyDockerfile := `FROM node:22-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install --production
COPY . .
RUN npm run build

FROM node:22-slim
WORKDIR /app
COPY --from=builder /app/dist ./dist
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \ CMD wget -q --spider http://127.0.0.1:3000/ || exit 0
USER 10001
CMD ["node", "dist/server.js"]
`

	healed := UniversalDockerfileHealer(buggyDockerfile, profile, tempDir)

	if strings.Contains(healed, "--production") {
		t.Fatalf("expected --production stripped from builder stage, got:\n%s", healed)
	}
	if strings.Contains(healed, `node", "dist/server.js`) || strings.Contains(healed, "server.js") {
		t.Fatalf("expected node dist/server.js replaced with serve, got:\n%s", healed)
	}
	if !strings.Contains(healed, `CMD ["serve", "-s", "dist", "-l", "tcp://0.0.0.0:3000"]`) {
		t.Fatalf("expected serve command in healed Dockerfile, got:\n%s", healed)
	}
	if !strings.Contains(healed, "npm install -g serve") {
		t.Fatalf("expected npm install -g serve injected, got:\n%s", healed)
	}
	if strings.Contains(healed, `\ CMD`) {
		t.Fatalf("expected backslash before CMD removed from HEALTHCHECK, got:\n%s", healed)
	}
}

func TestProjectDetector_NodeNoBuildScript(t *testing.T) {
	tempDir := t.TempDir()
	pkgJson := `{
		"name": "simple-api",
		"main": "server.js",
		"scripts": {
			"start": "node server.js"
		},
		"dependencies": {
			"express": "^4.18.2"
		}
	}`
	if err := os.WriteFile(filepath.Join(tempDir, "package.json"), []byte(pkgJson), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tempDir, "server.js"), []byte("console.log('hi');"), 0644); err != nil {
		t.Fatal(err)
	}

	profile := DetectProject(tempDir)
	if profile.HasBuildScript {
		t.Fatalf("expected HasBuildScript false")
	}
	if profile.AppType != AppTypeNodeServer {
		t.Fatalf("expected AppTypeNodeServer, got %v", profile.AppType)
	}

	// Test healing when Dockerfile erroneously called npm run build
	df := `FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build
CMD ["node", "server.js"]`

	healed := UniversalDockerfileHealer(df, profile, tempDir)
	if strings.Contains(healed, "RUN npm run build") && !strings.Contains(healed, "# [auto-healed") {
		t.Fatalf("expected npm run build neutralized because no build script, got:\n%s", healed)
	}
}

func TestProjectDetector_PythonFastAPI(t *testing.T) {
	tempDir := t.TempDir()
	reqs := "fastapi==0.109.0\nuvicorn==0.27.0\npydantic==2.6.0\n"
	if err := os.WriteFile(filepath.Join(tempDir, "requirements.txt"), []byte(reqs), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tempDir, "main.py"), []byte("from fastapi import FastAPI\napp = FastAPI()\n"), 0644); err != nil {
		t.Fatal(err)
	}

	profile := DetectProject(tempDir)
	if profile.PrimaryLanguage != LangPython {
		t.Fatalf("expected LangPython, got %v", profile.PrimaryLanguage)
	}
	if profile.Framework != "FastAPI" {
		t.Fatalf("expected FastAPI, got %v", profile.Framework)
	}
	if len(profile.StartCommand) == 0 || profile.StartCommand[0] != "uvicorn" {
		t.Fatalf("expected uvicorn start command, got %v", profile.StartCommand)
	}
}

func TestProjectDetector_Go(t *testing.T) {
	tempDir := t.TempDir()
	goMod := "module example.com/my-go-app\n\ngo 1.23\n"
	if err := os.WriteFile(filepath.Join(tempDir, "go.mod"), []byte(goMod), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tempDir, "main.go"), []byte("package main\nfunc main() {}\n"), 0644); err != nil {
		t.Fatal(err)
	}

	profile := DetectProject(tempDir)
	if profile.PrimaryLanguage != LangGo {
		t.Fatalf("expected LangGo, got %v", profile.PrimaryLanguage)
	}
	if profile.AppType != AppTypeGoBinary {
		t.Fatalf("expected AppTypeGoBinary, got %v", profile.AppType)
	}
}
