// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Language represents the detected programming language.
type Language string

const (
	LangNode      Language = "node"
	LangPython    Language = "python"
	LangGo        Language = "go"
	LangRust      Language = "rust"
	LangJava      Language = "java"
	LangPHP       Language = "php"
	LangRuby      Language = "ruby"
	LangDotNet    Language = "dotnet"
	LangStaticWeb Language = "static_web"
	LangUnknown   Language = "unknown"
)

// AppType represents the architectural type of the application.
type AppType string

const (
	AppTypeStaticSPA    AppType = "static_spa"
	AppTypeNodeServer   AppType = "node_server"
	AppTypeFullstackSSR AppType = "fullstack_ssr"
	AppTypePythonWeb    AppType = "python_web"
	AppTypeGoBinary     AppType = "go_binary"
	AppTypeRustBinary   AppType = "rust_binary"
	AppTypeJavaJar      AppType = "java_jar"
	AppTypeGeneric      AppType = "generic"
)

// ProjectProfile holds the comprehensive inspection result of a repository or workspace.
type ProjectProfile struct {
	BaseDir               string
	PrimaryLanguage       Language
	PackageManager        string
	Framework             string
	AppType               AppType
	HasBuildScript        bool
	BuildCommand          string
	BuildToolNeedsDevDeps bool
	BuildOutputDir        string
	DefaultPort           int
	StartCommand          []string
	StaticServeRequired   bool
	HasRootPackageJson    bool
	Subprojects           map[string]*ProjectProfile
	ManifestsFound        []string
}

// packageJsonStructure represents the relevant parts of a package.json file.
type packageJsonStructure struct {
	Name            string            `json:"name"`
	Scripts         map[string]string `json:"scripts"`
	Dependencies    map[string]string `json:"dependencies"`
	DevDependencies map[string]string `json:"devDependencies"`
	Workspaces      interface{}       `json:"workspaces"`
	Main            string            `json:"main"`
}

// DetectProject inspects baseDir and returns a ProjectProfile describing how the project
// must be built, containerized, and deployed.
func DetectProject(baseDir string) *ProjectProfile {
	profile := &ProjectProfile{
		BaseDir:         baseDir,
		PrimaryLanguage: LangUnknown,
		AppType:         AppTypeGeneric,
		DefaultPort:     3000,
		Subprojects:     make(map[string]*ProjectProfile),
	}

	// 1. Check for Node / JavaScript / TypeScript
	pkgJsonPath := filepath.Join(baseDir, "package.json")
	if pkgBytes, err := os.ReadFile(pkgJsonPath); err == nil {
		profile.HasRootPackageJson = true
		profile.PrimaryLanguage = LangNode
		profile.ManifestsFound = append(profile.ManifestsFound, "package.json")
		analyzeNodeProject(profile, pkgBytes, baseDir)
	}

	// Check package managers via lockfiles
	detectNodePackageManager(profile, baseDir)

	// 2. Check for Python
	if profile.PrimaryLanguage == LangUnknown {
		detectPythonProject(profile, baseDir)
	}

	// 3. Check for Go
	if profile.PrimaryLanguage == LangUnknown {
		detectGoProject(profile, baseDir)
	}

	// 4. Check for Rust
	if profile.PrimaryLanguage == LangUnknown {
		detectRustProject(profile, baseDir)
	}

	// 5. Check for Java
	if profile.PrimaryLanguage == LangUnknown {
		detectJavaProject(profile, baseDir)
	}

	// 6. Check for PHP
	if profile.PrimaryLanguage == LangUnknown {
		detectPHPProject(profile, baseDir)
	}

	// 7. Check for Ruby
	if profile.PrimaryLanguage == LangUnknown {
		detectRubyProject(profile, baseDir)
	}

	// 8. Check for .NET
	if profile.PrimaryLanguage == LangUnknown {
		detectDotNetProject(profile, baseDir)
	}

	// 9. Check for Static HTML without build tools
	if profile.PrimaryLanguage == LangUnknown {
		if _, err := os.Stat(filepath.Join(baseDir, "index.html")); err == nil {
			profile.PrimaryLanguage = LangStaticWeb
			profile.AppType = AppTypeStaticSPA
			profile.StaticServeRequired = true
			profile.BuildOutputDir = "."
			profile.DefaultPort = 3000
			profile.StartCommand = []string{"serve", "-s", ".", "-l", "tcp://0.0.0.0:3000"}
		}
	}

	// Inspect subprojects (e.g. monorepo / multi-tier layouts like frontend/ and backend/)
	entries, err := os.ReadDir(baseDir)
	if err == nil {
		for _, e := range entries {
			if e.IsDir() && !strings.HasPrefix(e.Name(), ".") && e.Name() != "node_modules" && e.Name() != "vendor" {
				subDir := filepath.Join(baseDir, e.Name())
				if subPkgBytes, err := os.ReadFile(filepath.Join(subDir, "package.json")); err == nil {
					subProf := &ProjectProfile{
						BaseDir:         subDir,
						PrimaryLanguage: LangNode,
						AppType:         AppTypeGeneric,
						DefaultPort:     3000,
						ManifestsFound:  []string{filepath.Join(e.Name(), "package.json")},
					}
					analyzeNodeProject(subProf, subPkgBytes, subDir)
					profile.Subprojects[e.Name()] = subProf
				}
			}
		}
	}

	// If root was unknown but subprojects exist, synthesize overall architecture
	if profile.PrimaryLanguage == LangUnknown && len(profile.Subprojects) > 0 {
		for name, sub := range profile.Subprojects {
			lowerName := strings.ToLower(name)
			if strings.Contains(lowerName, "front") || strings.Contains(lowerName, "client") || strings.Contains(lowerName, "ui") || strings.Contains(lowerName, "web") {
				profile.PrimaryLanguage = sub.PrimaryLanguage
				profile.AppType = sub.AppType
				profile.Framework = sub.Framework
				profile.DefaultPort = sub.DefaultPort
				break
			}
		}
	}

	return profile
}

func analyzeNodeProject(profile *ProjectProfile, pkgBytes []byte, dir string) {
	var pkg packageJsonStructure
	if err := json.Unmarshal(pkgBytes, &pkg); err != nil {
		return
	}

	allDeps := make(map[string]bool)
	for d := range pkg.Dependencies {
		allDeps[strings.ToLower(d)] = true
	}
	devDeps := make(map[string]bool)
	for d := range pkg.DevDependencies {
		devDeps[strings.ToLower(d)] = true
		allDeps[strings.ToLower(d)] = true
	}

	// Check build script
	if buildCmd, ok := pkg.Scripts["build"]; ok && strings.TrimSpace(buildCmd) != "" {
		profile.HasBuildScript = true
		profile.BuildCommand = "npm run build"
		profile.BuildToolNeedsDevDeps = true
	}

	// Identify build tools & frameworks
	isVite := devDeps["vite"] || allDeps["vite"] || fileExists(dir, "vite.config.ts") || fileExists(dir, "vite.config.js")
	isNext := allDeps["next"] || fileExists(dir, "next.config.js") || fileExists(dir, "next.config.mjs") || fileExists(dir, "next.config.ts")
	isNuxt := allDeps["nuxt"] || fileExists(dir, "nuxt.config.ts") || fileExists(dir, "nuxt.config.js")
	isAstro := allDeps["astro"] || fileExists(dir, "astro.config.mjs")
	isCreateReactApp := allDeps["react-scripts"]
	isVue := allDeps["vue"] || devDeps["@vue/cli-service"]
	isAngular := allDeps["@angular/core"] || fileExists(dir, "angular.json")
	isExpress := allDeps["express"] || allDeps["koa"] || allDeps["fastify"] || allDeps["@nestjs/core"]

	if isNext {
		profile.Framework = "Next.js"
		profile.AppType = AppTypeFullstackSSR
		profile.DefaultPort = 3000
		profile.StartCommand = []string{"npm", "start"}
	} else if isNuxt {
		profile.Framework = "Nuxt"
		profile.AppType = AppTypeFullstackSSR
		profile.DefaultPort = 3000
		profile.StartCommand = []string{"npm", "start"}
	} else if isVite || isCreateReactApp || (isVue && !isExpress) || (isAngular && !isExpress) || isAstro {
		if isVite {
			profile.Framework = "Vite"
		} else if isCreateReactApp {
			profile.Framework = "Create-React-App"
		} else if isAngular {
			profile.Framework = "Angular"
		} else if isVue {
			profile.Framework = "Vue"
		} else if isAstro {
			profile.Framework = "Astro"
		}

		profile.AppType = AppTypeStaticSPA
		profile.StaticServeRequired = true
		profile.DefaultPort = 3000

		// Output directory resolution
		profile.BuildOutputDir = "dist"
		if isCreateReactApp {
			profile.BuildOutputDir = "build"
		} else if isAngular {
			profile.BuildOutputDir = "dist"
		} else if isAstro {
			profile.BuildOutputDir = "dist"
		}
		if fileExists(dir, "build") && !fileExists(dir, "dist") {
			profile.BuildOutputDir = "build"
		}

		profile.StartCommand = []string{"serve", "-s", profile.BuildOutputDir, "-l", fmt.Sprintf("tcp://0.0.0.0:%d", profile.DefaultPort)}
	} else if isExpress {
		profile.Framework = "Express"
		profile.AppType = AppTypeNodeServer
		profile.DefaultPort = 3000

		serverEntry := findNodeEntrypoint(dir, pkg.Main)
		profile.StartCommand = []string{"node", serverEntry}
	} else {
		// Generic Node app: check if index.html exists at root -> static SPA
		if fileExists(dir, "index.html") {
			profile.AppType = AppTypeStaticSPA
			profile.StaticServeRequired = true
			profile.BuildOutputDir = "dist"
			if fileExists(dir, "build") {
				profile.BuildOutputDir = "build"
			}
			profile.StartCommand = []string{"serve", "-s", profile.BuildOutputDir, "-l", "tcp://0.0.0.0:3000"}
		} else {
			profile.AppType = AppTypeNodeServer
			serverEntry := findNodeEntrypoint(dir, pkg.Main)
			profile.StartCommand = []string{"node", serverEntry}
		}
	}
}

func detectNodePackageManager(profile *ProjectProfile, dir string) {
	if fileExists(dir, "pnpm-lock.yaml") {
		profile.PackageManager = "pnpm"
		profile.ManifestsFound = append(profile.ManifestsFound, "pnpm-lock.yaml")
	} else if fileExists(dir, "yarn.lock") {
		profile.PackageManager = "yarn"
		profile.ManifestsFound = append(profile.ManifestsFound, "yarn.lock")
	} else if fileExists(dir, "bun.lock") || fileExists(dir, "bun.lockb") {
		profile.PackageManager = "bun"
		profile.ManifestsFound = append(profile.ManifestsFound, "bun.lock")
	} else if fileExists(dir, "package-lock.json") {
		profile.PackageManager = "npm"
		profile.ManifestsFound = append(profile.ManifestsFound, "package-lock.json")
	} else if profile.PrimaryLanguage == LangNode {
		profile.PackageManager = "npm"
	}
}

func detectPythonProject(profile *ProjectProfile, dir string) {
	hasReq := fileExists(dir, "requirements.txt")
	hasPyproject := fileExists(dir, "pyproject.toml")
	hasPipfile := fileExists(dir, "Pipfile")

	if !hasReq && !hasPyproject && !hasPipfile && !fileExists(dir, "setup.py") && !fileExists(dir, "main.py") && !fileExists(dir, "app.py") && !fileExists(dir, "manage.py") {
		return
	}

	profile.PrimaryLanguage = LangPython
	profile.AppType = AppTypePythonWeb
	profile.DefaultPort = 8000
	profile.PackageManager = "pip"

	if fileExists(dir, "poetry.lock") {
		profile.PackageManager = "poetry"
		profile.ManifestsFound = append(profile.ManifestsFound, "poetry.lock")
	} else if fileExists(dir, "uv.lock") {
		profile.PackageManager = "uv"
		profile.ManifestsFound = append(profile.ManifestsFound, "uv.lock")
	} else if fileExists(dir, "Pipfile.lock") {
		profile.PackageManager = "pipenv"
		profile.ManifestsFound = append(profile.ManifestsFound, "Pipfile.lock")
	} else if hasReq {
		profile.ManifestsFound = append(profile.ManifestsFound, "requirements.txt")
	}

	// Detect Framework
	if fileExists(dir, "manage.py") {
		profile.Framework = "Django"
		profile.DefaultPort = 8000
		profile.StartCommand = []string{"python", "manage.py", "runserver", "0.0.0.0:8000"}
	} else {
		entry := "main.py"
		if fileExists(dir, "app.py") {
			entry = "app.py"
		} else if fileExists(dir, "server.py") {
			entry = "server.py"
		}

		// Read requirements.txt or pyproject to see if fastapi / flask is declared
		reqContent := ""
		if b, err := os.ReadFile(filepath.Join(dir, "requirements.txt")); err == nil {
			reqContent = strings.ToLower(string(b))
		}
		if b, err := os.ReadFile(filepath.Join(dir, "pyproject.toml")); err == nil {
			reqContent += strings.ToLower(string(b))
		}

		if strings.Contains(reqContent, "fastapi") || strings.Contains(reqContent, "uvicorn") {
			profile.Framework = "FastAPI"
			moduleName := strings.TrimSuffix(entry, ".py")
			profile.StartCommand = []string{"uvicorn", fmt.Sprintf("%s:app", moduleName), "--host", "0.0.0.0", "--port", "8000"}
		} else if strings.Contains(reqContent, "flask") {
			profile.Framework = "Flask"
			profile.StartCommand = []string{"python", entry}
		} else {
			profile.StartCommand = []string{"python", entry}
		}
	}
}

func detectGoProject(profile *ProjectProfile, dir string) {
	if !fileExists(dir, "go.mod") && !fileExists(dir, "main.go") {
		return
	}
	profile.PrimaryLanguage = LangGo
	profile.AppType = AppTypeGoBinary
	profile.PackageManager = "go modules"
	profile.DefaultPort = 8080
	profile.HasBuildScript = true
	profile.BuildCommand = "CGO_ENABLED=0 go build -o /app/server ."
	profile.StartCommand = []string{"/app/server"}
	profile.ManifestsFound = append(profile.ManifestsFound, "go.mod")
}

func detectRustProject(profile *ProjectProfile, dir string) {
	if !fileExists(dir, "Cargo.toml") {
		return
	}
	profile.PrimaryLanguage = LangRust
	profile.AppType = AppTypeRustBinary
	profile.PackageManager = "cargo"
	profile.DefaultPort = 8080
	profile.HasBuildScript = true
	profile.BuildCommand = "cargo build --release"
	profile.StartCommand = []string{"/app/server"}
	profile.ManifestsFound = append(profile.ManifestsFound, "Cargo.toml")
}

func detectJavaProject(profile *ProjectProfile, dir string) {
	hasPom := fileExists(dir, "pom.xml")
	hasGradle := fileExists(dir, "build.gradle") || fileExists(dir, "build.gradle.kts")
	if !hasPom && !hasGradle {
		return
	}
	profile.PrimaryLanguage = LangJava
	profile.AppType = AppTypeJavaJar
	profile.DefaultPort = 8080
	profile.HasBuildScript = true

	if hasPom {
		profile.PackageManager = "maven"
		profile.ManifestsFound = append(profile.ManifestsFound, "pom.xml")
		if fileExists(dir, "mvnw") {
			profile.BuildCommand = "./mvnw clean package -DskipTests"
		} else {
			profile.BuildCommand = "mvn clean package -DskipTests"
		}
	} else {
		profile.PackageManager = "gradle"
		profile.ManifestsFound = append(profile.ManifestsFound, "build.gradle")
		if fileExists(dir, "gradlew") {
			profile.BuildCommand = "./gradlew build -x test"
		} else {
			profile.BuildCommand = "gradle build -x test"
		}
	}
	profile.StartCommand = []string{"java", "-jar", "/app/app.jar"}
}

func detectPHPProject(profile *ProjectProfile, dir string) {
	if !fileExists(dir, "composer.json") && !fileExists(dir, "index.php") {
		return
	}
	profile.PrimaryLanguage = LangPHP
	profile.PackageManager = "composer"
	profile.DefaultPort = 80
	if fileExists(dir, "composer.json") {
		profile.ManifestsFound = append(profile.ManifestsFound, "composer.json")
	}
	profile.StartCommand = []string{"php", "-S", "0.0.0.0:80", "-t", "public"}
}

func detectRubyProject(profile *ProjectProfile, dir string) {
	if !fileExists(dir, "Gemfile") {
		return
	}
	profile.PrimaryLanguage = LangRuby
	profile.PackageManager = "bundler"
	profile.DefaultPort = 3000
	profile.ManifestsFound = append(profile.ManifestsFound, "Gemfile")
	profile.StartCommand = []string{"bundle", "exec", "rackup", "-o", "0.0.0.0", "-p", "3000"}
}

func detectDotNetProject(profile *ProjectProfile, dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".csproj") || strings.HasSuffix(e.Name(), ".sln") {
			profile.PrimaryLanguage = LangDotNet
			profile.PackageManager = "dotnet"
			profile.DefaultPort = 5000
			profile.HasBuildScript = true
			profile.BuildCommand = "dotnet publish -c Release -o /app/out"
			profile.StartCommand = []string{"dotnet", "/app/out/app.dll"}
			profile.ManifestsFound = append(profile.ManifestsFound, e.Name())
			return
		}
	}
}

// nodeInstallCmd is the dependency install for a package manager. It is the builder-stage form:
// devDependencies stay installed because the build CLI (tsc, vite, webpack, next) lives there.
func nodeInstallCmd(pm string) string {
	switch pm {
	case "pnpm":
		return "corepack enable && pnpm install"
	case "yarn":
		return "corepack enable && yarn install"
	case "bun":
		return "npm install -g bun && bun install"
	default:
		return "npm install"
	}
}

// nodeBuildCmd is the build for a package manager, or "" when the project declares no build step.
func nodeBuildCmd(pm string) string {
	switch pm {
	case "pnpm":
		return "pnpm run build"
	case "yarn":
		return "yarn run build"
	case "bun":
		return "bun run build"
	default:
		return "npm run build"
	}
}

func jsonCmd(cmd []string) string {
	if len(cmd) == 0 {
		return `[]`
	}
	encoded, err := json.Marshal(cmd)
	if err != nil {
		return `[]`
	}
	return string(encoded)
}

// GenerateDockerfile renders a buildable Dockerfile from a detected profile. It is the other half of
// supporting arbitrary applications: a repository that ships no Dockerfile still has to deploy, so the
// platform supplies the container build the project never declared. Every branch is chosen from the
// profile, so a new language is supported by teaching DetectProject about it, not by editing this.
func GenerateDockerfile(profile *ProjectProfile) string {
	port := profile.DefaultPort
	if port <= 0 {
		port = 3000
	}

	switch profile.PrimaryLanguage {
	case LangNode:
		pm := profile.PackageManager
		build := ""
		if profile.HasBuildScript {
			build = "\nRUN " + nodeBuildCmd(pm)
		}
		install := nodeInstallCmd(pm)

		switch profile.AppType {
		case AppTypeStaticSPA:
			out := profile.BuildOutputDir
			if out == "" {
				out = "dist"
			}
			return fmt.Sprintf(`FROM node:22-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN %s
COPY . .
RUN %s

FROM nginx:alpine
COPY --from=builder /app/%s /usr/share/nginx/html
EXPOSE %d
`, install, nodeBuildCmd(pm), out, port)

		case AppTypeFullstackSSR:
			return fmt.Sprintf(`FROM node:22-slim
WORKDIR /app
COPY package*.json ./
RUN %s
COPY . .
RUN %s
EXPOSE %d
CMD %s
`, install, nodeBuildCmd(pm), port, jsonCmd(profile.StartCommand))

		default: // AppTypeNodeServer and anything else Node that runs a process
			start := profile.StartCommand
			if len(start) == 0 {
				start = []string{"node", findNodeEntrypoint(profile.BaseDir, "")}
			}
			// No build step means nothing needs the devDependencies, so the image can drop them.
			if !profile.HasBuildScript {
				install = strings.Replace(install, " install", " install --omit=dev", 1)
			}
			return fmt.Sprintf(`FROM node:22-slim
WORKDIR /app
COPY package*.json ./
RUN %s
COPY . .%s
EXPOSE %d
CMD %s
`, install, build, port, jsonCmd(start))
		}

	case LangPython:
		install := "if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi"
		entry := ""
		switch profile.PackageManager {
		case "poetry":
			install = "pip install --no-cache-dir poetry && poetry install --no-interaction --no-root"
		case "uv":
			install = "pip install --no-cache-dir uv && uv sync --frozen || uv sync"
		case "pipenv":
			install = "pip install --no-cache-dir pipenv && pipenv install --system --deploy"
		}
		if fileExists(profile.BaseDir, "pyproject.toml") && profile.PackageManager == "pip" {
			install = "if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi; " +
				"if [ -f pyproject.toml ]; then pip install --no-cache-dir . || true; fi"
		}
		start := profile.StartCommand
		if len(start) == 0 {
			start = []string{"python", "main.py"}
		}
		return fmt.Sprintf(`FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN %s
EXPOSE %d
%s
CMD %s
`, install, port, entry, jsonCmd(start))

	case LangGo:
		return fmt.Sprintf(`FROM golang:1.23-alpine AS builder
WORKDIR /app
COPY go.mod go.sum* ./
RUN go mod download || true
COPY . .
RUN CGO_ENABLED=0 go build -o /app/server .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
WORKDIR /app
COPY --from=builder /app/server /app/server
EXPOSE %d
CMD ["/app/server"]
`, port)

	case LangRust:
		binary := "server"
		if len(profile.StartCommand) > 0 {
			binary = filepath.Base(profile.StartCommand[0])
		}
		return fmt.Sprintf(`FROM rust:1.82-slim AS builder
WORKDIR /app
COPY . .
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /app/target/release/%s /app/%s
EXPOSE %d
CMD ["/app/%s"]
`, binary, binary, port, binary)

	case LangJava:
		if profile.PackageManager == "gradle" {
			return fmt.Sprintf(`FROM gradle:8-jdk21 AS builder
WORKDIR /app
COPY . .
RUN gradle build -x test --no-daemon

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=builder /app/build/libs /app/libs
EXPOSE %d
CMD ["sh", "-c", "java -jar $(ls /app/libs/*.jar | head -n 1)"]
`, port)
		}
		return fmt.Sprintf(`FROM maven:3.9-eclipse-temurin-21 AS builder
WORKDIR /app
COPY pom.xml .
RUN mvn -q dependency:go-offline || true
COPY . .
RUN mvn clean package -DskipTests

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=builder /app/target/*.jar /app/
EXPOSE %d
CMD ["sh", "-c", "java -jar $(ls /app/*.jar | head -n 1)"]
`, port)

	case LangPHP:
		return fmt.Sprintf(`FROM composer:2 AS vendor
WORKDIR /app
COPY composer.json composer.lock* ./
RUN composer install --no-dev --no-interaction --no-scripts --ignore-platform-reqs || true

FROM php:8.3-cli
WORKDIR /app
COPY --from=vendor /app/vendor /app/vendor
COPY . .
EXPOSE %d
CMD %s
`, port, jsonCmd(profile.StartCommand))

	case LangRuby:
		return fmt.Sprintf(`FROM ruby:3.3-slim
WORKDIR /app
COPY Gemfile* ./
RUN bundle install || true
COPY . .
EXPOSE %d
CMD %s
`, port, jsonCmd(profile.StartCommand))

	case LangDotNet:
		return fmt.Sprintf(`FROM mcr.microsoft.com/dotnet/sdk:8.0 AS builder
WORKDIR /app
COPY . .
RUN dotnet publish -c Release -o /app/out

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=builder /app/out .
EXPOSE %d
CMD ["sh", "-c", "dotnet $(ls *.dll | grep -v -i 'ref/' | head -n 1)"]
`, port)

	case LangStaticWeb:
		return fmt.Sprintf(`FROM nginx:alpine
COPY . /usr/share/nginx/html
EXPOSE %d
`, port)
	}

	// Nothing recognised. Returning empty is the honest answer: the caller reports that no build
	// strategy was detected rather than inventing one and failing later inside a container build.
	return ""
}

func findNodeEntrypoint(dir string, mainField string) string {
	if mainField != "" && fileExists(dir, mainField) {
		return mainField
	}
	for _, cand := range []string{"server.js", "app.js", "index.js", "src/server.js", "src/index.js", "dist/server.js", "dist/index.js"} {
		if fileExists(dir, cand) {
			return cand
		}
	}
	return "server.js"
}

func fileExists(dir, relPath string) bool {
	_, err := os.Stat(filepath.Join(dir, relPath))
	return err == nil
}

// UniversalDockerfileHealer inspects existing Dockerfile content and transforms it into a
// robust, working multi-stage build tailored to the detected project profile.
func UniversalDockerfileHealer(content string, profile *ProjectProfile, baseDir string) string {
	lines := strings.Split(content, "\n")
	var resultLines []string

	// Track whether we are in a builder stage
	inBuilderStage := false
	hasBuildScript := profile.HasBuildScript

	// Only treat as a static frontend when the ROOT project itself is identified as a
	// static SPA with its own package.json. In monorepo layouts (no root package.json,
	// only subprojects), the downstream healer in deployment.go handles CMD and serve
	// injection with its own frontend directory detection.
	isStaticFrontend := (profile.StaticServeRequired || profile.AppType == AppTypeStaticSPA) &&
		profile.HasRootPackageJson

	// Check whether any subproject has a build script (monorepo scenario).
	// When subprojects exist, the downstream healer transforms bare "npm run build"
	// into "(cd Frontent && npm run build)", so the universal healer must not touch it.
	anySubprojectHasBuild := false
	for _, sub := range profile.Subprojects {
		if sub.HasBuildScript {
			anySubprojectHasBuild = true
			break
		}
	}
	hasSubprojects := len(profile.Subprojects) > 0

	// Check whether dist / build folder exists on disk or will be built
	distTarget := profile.BuildOutputDir
	if distTarget == "" {
		distTarget = "dist"
	}

	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		upper := strings.ToUpper(trimmed)

		// 1. Detect build stage
		if strings.HasPrefix(upper, "FROM ") {
			if strings.Contains(upper, " AS BUILDER") || strings.Contains(upper, " AS BUILD") {
				inBuilderStage = true
			} else {
				inBuilderStage = false
			}
		}

		// 2. BUILDER DEPENDENCY RULE:
		// If in builder stage (or before npm run build), NEVER strip devDependencies with --production or --omit=dev!
		// Build CLI tools (vite, tsc, esbuild, next, webpack) are in devDependencies. Stripping them causes exit code 127!
		if inBuilderStage || strings.Contains(line, "npm install") || strings.Contains(line, "npm ci") || strings.Contains(line, "pnpm install") || strings.Contains(line, "yarn install") {
			// In builder stage or prior to build: strip flags that drop devDependencies
			line = strings.ReplaceAll(line, " --production", "")
			line = strings.ReplaceAll(line, "--production", "")
			line = strings.ReplaceAll(line, " --omit=dev", "")
			line = strings.ReplaceAll(line, "--omit=dev", "")
			line = strings.ReplaceAll(line, " --frozen-lockfile", "")
			line = strings.ReplaceAll(line, "--frozen-lockfile", "")
		}

		// 3. BUILD COMMAND SANITY RULE:
		// If npm run build is called but package.json has NO build script, skip it.
		// BUT: do NOT touch lines that are cd-prefixed monorepo commands (e.g. "cd Frontent && npm run build")
		// or lines with "|| true" (already guarded), or when subprojects exist (the downstream healer
		// will transform bare "npm run build" into the correct subproject build).
		if strings.Contains(line, "npm run build") {
			isCdPrefixed := strings.Contains(line, "cd ") && strings.Contains(line, "&&")
			isGuarded := strings.Contains(line, "|| true")
			if !hasBuildScript && profile.PrimaryLanguage == LangNode &&
				!isCdPrefixed && !isGuarded &&
				!hasSubprojects && !anySubprojectHasBuild {
				line = "# [auto-healed: skipped build command because package.json declares no build script] true"
			}
		}

		// 4. MULTI-STAGE COPY SANITY:
		// Fix relative paths: COPY --from=builder ./dist ./ -> COPY --from=builder /app/dist ./
		if strings.HasPrefix(upper, "COPY ") && strings.Contains(upper, "--FROM=") {
			reCopy := regexp.MustCompile(`(?i)COPY\s+--from=([a-zA-Z0-9_-]+)\s+(\.\/\S+|\bdist\b|\bbuild\b|\bpublic\b|\bout\b)\s+(\S+)`)
			line = reCopy.ReplaceAllStringFunc(line, func(m string) string {
				parts := reCopy.FindStringSubmatch(m)
				if len(parts) >= 4 {
					stage := parts[1]
					src := strings.TrimPrefix(parts[2], "./")
					dest := parts[3]
					if !strings.HasPrefix(src, "/") {
						src = "/app/" + src
					}
					return fmt.Sprintf("COPY --from=%s %s %s", stage, src, dest)
				}
				return m
			})
			line = strings.ReplaceAll(line, "COPY --from=builder ./ ", "COPY --from=builder /app/ ")
			line = strings.ReplaceAll(line, "COPY --from=builder . ", "COPY --from=builder /app/ ")
		}

		// 5. RUNTIME STAGE ENTRYPOINT SANITY:
		// If application is a static SPA (e.g. Vite, React, Vue, HTML), it has NO server.js!
		// Hallucinated commands like `CMD ["node", "dist/server.js"]` or `CMD ["node", "server.js"]` crash immediately.
		// Only apply when the root project is definitively a static SPA (not monorepo subproject detection).
		if isStaticFrontend && strings.HasPrefix(upper, "CMD ") {
			if strings.Contains(line, "server.js") || strings.Contains(line, "Backend/server.js") || strings.Contains(line, "node dist/") || strings.Contains(line, `node", "dist`) {
				serveCmd := fmt.Sprintf(`CMD ["serve", "-s", "%s", "-l", "tcp://0.0.0.0:3000"]`, distTarget)
				line = serveCmd
			}
		}

		// 6. HEALTHCHECK SANITY:
		// Remove syntax errors like stray backslashes before CMD
		if strings.HasPrefix(upper, "HEALTHCHECK ") {
			reHealthBackslash := regexp.MustCompile(`(?i)HEALTHCHECK\s+([\s\S]*?)\s*\\\s*CMD\s+([^\n]+)`)
			line = reHealthBackslash.ReplaceAllString(line, "HEALTHCHECK $1 CMD $2")
		}

		resultLines = append(resultLines, line)
	}

	healed := strings.Join(resultLines, "\n")

	// If it's a static frontend and serve command is used, ensure `serve` is installed in the final image
	if isStaticFrontend && strings.Contains(healed, "serve -s") && !strings.Contains(healed, "serve") {
		// Inject serve installation before CMD
		reCmd := regexp.MustCompile(`(?m)^CMD\s+.*`)
		if reCmd.MatchString(healed) {
			healed = reCmd.ReplaceAllString(healed, "RUN npm install -g serve\n$0")
		}
	} else if isStaticFrontend && strings.Contains(healed, `"serve"`) && !strings.Contains(healed, "npm install -g serve") {
		reUser := regexp.MustCompile(`(?m)^USER\s+.*`)
		if reUser.MatchString(healed) {
			healed = reUser.ReplaceAllString(healed, "RUN npm install -g serve\n$0")
		} else {
			reCmd := regexp.MustCompile(`(?m)^CMD\s+.*`)
			if reCmd.MatchString(healed) {
				healed = reCmd.ReplaceAllString(healed, "RUN npm install -g serve\n$0")
			}
		}
	}

	return healed
}
