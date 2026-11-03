// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

func TestClassifyErrorDeterministicCompilation(t *testing.T) {
	tsError := "src/index.ts(12,5): error TS2307: Cannot find module './missing' or its corresponding type declarations."
	err := ClassifyError("build", 1, "", tsError)

	if err.Class != ErrorClassDeterministic {
		t.Fatalf("expected ErrorClassDeterministic, got %s", err.Class)
	}
	if err.ExitCode != 1 {
		t.Fatalf("expected exit code 1, got %d", err.ExitCode)
	}
}

func TestClassifyErrorTransient(t *testing.T) {
	netTimeout := "Get \"https://registry-1.docker.io/v2/\": dial tcp 140.82.121.3:443: i/o timeout"
	err := ClassifyError("build", 1, "", netTimeout)

	if err.Class != ErrorClassTransient {
		t.Fatalf("expected ErrorClassTransient, got %s", err.Class)
	}
}

func TestClassifyErrorApplicationCode(t *testing.T) {
	panicMsg := "panic: runtime error: invalid memory address or nil pointer dereference\ngoroutine 1 [running]:\nmain.main()"
	err := ClassifyError("startup", 2, "", panicMsg)

	if err.Class != ErrorClassApplicationCode {
		t.Fatalf("expected ErrorClassApplicationCode, got %s", err.Class)
	}
}

func TestClassifyErrorMissingExecutable(t *testing.T) {
	testCases := []struct {
		name     string
		exitCode int
		log      string
	}{
		{
			name:     "sh not found with exit 127",
			exitCode: 127,
			log:      "0.349 sh: 1: next: not found\nnpm error code 127\nERROR: process \"/bin/sh -c npm run build\" did not complete successfully: exit code: 127",
		},
		{
			name:     "bin sh not found",
			exitCode: 1,
			log:      "/bin/sh: 1: vite: not found",
		},
		{
			name:     "bash command not found",
			exitCode: 127,
			log:      "bash: cargo: command not found",
		},
		{
			name:     "executable file not found in path",
			exitCode: 1,
			log:      "exec: \"gradle\": executable file not found in $PATH",
		},
		{
			name:     "cache key not found",
			exitCode: 1,
			log:      "failed to compute cache key: \"/app/dist\" not found: not found",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			err := ClassifyError("build", tc.exitCode, "", tc.log)
			if err.Class != ErrorClassDeterministic {
				t.Fatalf("expected ErrorClassDeterministic for %s, got %s", tc.name, err.Class)
			}
		})
	}
}

func TestClassifyErrorTransientWithNotFoundText(t *testing.T) {
	// A network error containing "not found" (e.g. host not found / DNS failure) must still classify as transient
	transientDNS := "dial tcp: lookup registry-1.docker.io: temporary failure in name resolution: host not found"
	err := ClassifyError("build", 1, "", transientDNS)
	if err.Class != ErrorClassTransient {
		t.Fatalf("expected ErrorClassTransient, got %s", err.Class)
	}
}

func TestVerifyConsistencyGate(t *testing.T) {
	tmpDir, err := os.MkdirTemp("", "g3-test-*")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(tmpDir)

	dfPath := filepath.Join(tmpDir, "Dockerfile")
	dfContent := "FROM node:20\nWORKDIR /app\nCOPY non_existent_file.txt /app/\nEXPOSE 3000\nCMD [\"npm\", \"start\"]\n"
	if err := os.WriteFile(dfPath, []byte(dfContent), 0644); err != nil {
		t.Fatal(err)
	}

	bp := &models.ProjectBlueprint{
		BlueprintID:    "test-bp",
		RepositoryRoot: tmpDir,
	}

	err = VerifyConsistencyGate(tmpDir, dfPath, bp)
	if err == nil {
		t.Fatalf("expected G3 consistency gate to fail for non_existent_file.txt, but it passed")
	}
}
