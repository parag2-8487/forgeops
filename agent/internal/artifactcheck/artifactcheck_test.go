// SPDX-License-Identifier: Apache-2.0

package artifactcheck

import (
	"context"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

// TestOnlyArtifactsThisPackageCanValidateAreSelected pins the classifier.
//
// A misclassification is not harmless: handing a Go source file to kubeconform produces a failure that
// says the repository is broken when it is the classifier that is broken, and a reader cannot tell those
// apart from the report.
func TestOnlyArtifactsThisPackageCanValidateAreSelected(t *testing.T) {
	cases := []struct {
		path string
		want string
	}{
		{"docker-compose.yml", KindCompose},
		{"docker-compose.prod.yaml", KindCompose},
		{"compose.yaml", KindCompose},
		{"k8s/deployment.yaml", KindK8s},
		{"kubernetes/service.yml", KindK8s},
		{"manifests/ingress.yaml", KindK8s},
		{"deploy/web.yaml", KindK8s},
		{"charts/app/Chart.yaml", KindHelm},
		{".github/workflows/ci.yml", KindYAML},

		// Not artifacts this package can judge. Each of these was a candidate for being handed to the
		// wrong tool, which is why they are asserted rather than assumed.
		{"src/main.go", ""},
		{"README.md", ""},
		{"requirements.txt", ""},
		{"docker-compose.md", ""},
		{"k8s/notes.txt", ""},
		{"config/app.yaml", ""},
	}

	for _, tc := range cases {
		if got := kindOf(tc.path); got != tc.want {
			t.Errorf("kindOf(%q) = %q, want %q", tc.path, got, tc.want)
		}
	}
}

// TestTheOrderIsStable is the determinism the inventory hash depends on.
//
// Map iteration order would reorder the groups between two identical scans, so two scans of one tree
// would produce two different reports and the score's reproducibility claim would be false.
func TestTheOrderIsStable(t *testing.T) {
	paths := []string{
		"k8s/deployment.yaml", "docker-compose.yml", "charts/a/Chart.yaml", ".github/workflows/ci.yml",
	}
	first := classify(paths)
	for range 10 {
		next := classify(paths)
		if len(next) != len(first) {
			t.Fatalf("group count changed: %d then %d", len(first), len(next))
		}
		for i := range first {
			if first[i].kind != next[i].kind {
				t.Fatalf("group %d reordered: %q then %q", i, first[i].kind, next[i].kind)
			}
		}
	}
}

// TestTheCapIsPerKind means a monorepo full of manifests still gets its compose file checked.
//
// A global cap applied in path order would spend the whole budget on the first kind encountered, and a
// repository with 30 manifests would never have its compose file validated at all.
func TestTheCapIsPerKind(t *testing.T) {
	var paths []string
	for i := range MaxArtifacts + 10 {
		paths = append(paths, "k8s/deployment-"+string(rune('a'+i%26))+".yaml")
	}
	paths = append(paths, "docker-compose.yml")

	groups := classify(paths)
	byKind := map[string]int{}
	for _, group := range groups {
		byKind[group.kind] = len(group.paths)
	}
	if byKind[KindK8s] != MaxArtifacts {
		t.Errorf("k8s group holds %d, want the cap %d", byKind[KindK8s], MaxArtifacts)
	}
	if byKind[KindCompose] != 1 {
		t.Errorf("the compose file was crowded out: compose group holds %d, want 1", byKind[KindCompose])
	}
}

// TestAMissingToolIsReportedNotPassed is the whole point of the package.
//
// internal/validator exists because the previous implementations were substring matching wearing a
// validator's name and passed anything they did not understand. Reporting a missing tool as a pass here
// would re-introduce that defect one layer up, where it would be harder to see.
func TestAMissingToolIsReportedNotPassed(t *testing.T) {
	// A REAL runner over a real temporary directory. Whether `docker` happens to be installed on the
	// machine running this test is not the point being proved: either the tool is absent (tool_missing) or
	// it is present and cannot find the file (errored). The assertion is that NEITHER is a pass.
	runner := &validator.Runner{Dir: t.TempDir()}
	entry := validateOne(
		context.Background(), runner, KindCompose, "docker-compose.yml", "/nonexistent/docker-compose.yml",
	)

	if entry.Status == StatusPassed {
		t.Fatal("an artifact whose tool is absent was reported as passing")
	}
	if entry.Status != StatusToolMissing && entry.Status != StatusErrored {
		t.Fatalf("status %q is neither tool_missing nor errored", entry.Status)
	}
	if entry.Detail == "" {
		t.Error("no detail, so a user cannot tell which tool to install")
	}
	if entry.Path != "docker-compose.yml" {
		t.Errorf("path %q is not the repository-relative path", entry.Path)
	}
}

// TestTheDetailIsBounded keeps a chatty tool from writing a novel into the database.
func TestTheDetailIsBounded(t *testing.T) {
	long := strings.Repeat("x", DetailLimit*3)
	// The ellipsis is a multi-byte rune, so the byte length is the limit plus its encoded size. Asserting
	// against `len(...)+1` would be asserting against a one-byte ellipsis that does not exist.
	ellipsis := "\u2026"
	if got := truncate(long); len(got) > DetailLimit+len(ellipsis) {
		t.Errorf("truncate produced %d bytes, want at most %d", len(got), DetailLimit+len(ellipsis))
	}
	if !strings.HasSuffix(truncate(long), ellipsis) {
		t.Error("a truncated detail does not say it was truncated")
	}
	if truncate("short") != "short" {
		t.Error("a short detail was altered")
	}
}
