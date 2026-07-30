// SPDX-License-Identifier: Apache-2.0

//go:build tools

// Package tools pins the Go development tools by module version so `go run`
// resolves them through go.sum (design.md §0.5 debt D4, §8.4, §16.1).
//
// Why a separate module rather than agent/go.mod
// ----------------------------------------------
// A tool dependency in the shipped module joins the graph that D-1's cgo guard,
// `scripts/check-go-module.sh` and the release SBOM all police. Keeping the tools in
// `agent/tools/` means the agent's dependency set stays exactly what the binary
// needs, while the tools are still checksum-verified.
//
// Why `go run` rather than `go install`
// ------------------------------------
// Phase 0 ran `go install golang.org/x/vuln/cmd/govulncheck@latest`, which resolves
// at run time to whatever the proxy serves and is verified against nothing. It also
// ran `golangci-lint@v1.62.2`, where the tag is a mutable pointer. `go run` from
// inside this module resolves both from `go.mod` and verifies them against the
// committed `go.sum`, so the tool that gates the build is itself pinned by hash.
//
// Building golangci-lint from source here has a second benefit the CI comment
// already recorded: the prebuilt release binaries are compiled with an older
// toolchain and refuse to analyse a go1.26 module. Compiling with the repository's
// own Go removes that failure mode.
//
// The build tag means these imports never compile into anything; they exist so
// `go mod tidy` keeps the versions in go.mod.
package tools

import (
	_ "github.com/golangci/golangci-lint/cmd/golangci-lint"
	_ "golang.org/x/vuln/cmd/govulncheck"
)
