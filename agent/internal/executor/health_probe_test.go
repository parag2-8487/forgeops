// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The probe must be a command the image can RUN, and it must be able to FAIL.
//
// Both halves come from one real deployment. The generated Dockerfile carried
// `HEALTHCHECK ... CMD wget -q --spider ... || exit 0` against a `node:22-slim` base, and the container's
// own health log recorded `"/bin/sh: 1: wget: not found"` with `ExitCode: 0` — the tool was absent, so
// nothing was tested, and the `|| exit 0` reported success anyway. The deployment was then marked
// `degraded` ("at least one workload did not converge") while the application served HTTP 200 to every
// request it received.
func TestHealthProbeUsesAToolTheImageHas(t *testing.T) {
	cases := []struct {
		name     string
		language Language
		docker   string
		want     string
		absent   string
	}{
		{
			name:     "node uses its own fetch",
			language: LangNode,
			want:     `node -e "fetch(`,
			absent:   "wget",
		},
		{
			name:     "python uses the standard library",
			language: LangPython,
			want:     "urllib.request",
			absent:   "wget",
		},
		{
			name:     "node inferred from the base image when the profile is unknown",
			language: LangUnknown,
			docker:   "FROM node:22-slim\nWORKDIR /app\nCMD [\"node\", \"server.js\"]\n",
			want:     `node -e "fetch(`,
			absent:   "wget",
		},
		{
			name:     "python inferred from the base image",
			language: LangUnknown,
			docker:   "FROM python:3.12-slim\nCMD [\"uvicorn\", \"main:app\"]\n",
			want:     "urllib.request",
			absent:   "wget",
		},
		{
			name:     "node inferred from the start command alone",
			language: LangUnknown,
			docker:   "FROM somecorp/base:1\nCMD [\"node\", \"dist/index.js\"]\n",
			want:     `node -e "fetch(`,
			absent:   "wget",
		},
		{
			// An unrecognised image gets the honest answer rather than a plausible wrong one: a probe
			// whose tool is absent is precisely the defect this replaced.
			name:     "an unrecognised image claims nothing",
			language: LangUnknown,
			docker:   "FROM scratch\nCOPY app /app\n",
			want:     "true",
			absent:   "wget",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := healthProbeCommand(tc.language, 3000, tc.docker)
			if !strings.Contains(got, tc.want) {
				t.Errorf("expected %q in %q", tc.want, got)
			}
			if strings.Contains(got, tc.absent) {
				t.Errorf("%q is not guaranteed present in this image: %q", tc.absent, got)
			}
			// THE HALF THAT MAKES IT A CHECK AT ALL.
			if strings.Contains(got, "|| exit 0") || strings.Contains(got, "|| true") {
				t.Errorf("a probe that cannot fail reports healthy on a dead service: %q", got)
			}
		})
	}
}

func TestHealthProbeUsesTheServicesOwnPort(t *testing.T) {
	got := healthProbeCommand(LangNode, 8799, "")
	if !strings.Contains(got, "127.0.0.1:8799") {
		t.Errorf("the probe does not use the detected port: %q", got)
	}
	// A loopback probe, not 0.0.0.0 and not the external hostname: the check runs INSIDE the container.
	if strings.Contains(got, "0.0.0.0") || strings.Contains(got, "localhost:") {
		t.Errorf("the probe should address loopback by address: %q", got)
	}
}

// The healer must not reintroduce the defect on a project whose language it could not detect from a
// manifest — the case that produced the report, since the probe is chosen from the profile.
func TestHealerNeverInjectsWget(t *testing.T) {
	dir := t.TempDir()
	dockerfile := `FROM node:22-slim
WORKDIR /app
COPY . .
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD node -e "process.exit(0)"
CMD ["node", "server.js"]
`
	if err := os.WriteFile(filepath.Join(dir, "Dockerfile"), []byte(dockerfile), 0644); err != nil {
		t.Fatal(err)
	}
	autoHealDockerfileForCompose(dir, SinkFunc(func(int, string, string) {}))

	got := readFileOrEmpty(filepath.Join(dir, "Dockerfile"))
	if strings.Contains(got, "wget") {
		t.Errorf("the healer injected wget into a node:*-slim image again:\n%s", got)
	}
	if !strings.Contains(got, "HEALTHCHECK") {
		t.Errorf("the healer removed the HEALTHCHECK entirely:\n%s", got)
	}
}
