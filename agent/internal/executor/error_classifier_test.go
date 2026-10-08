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
