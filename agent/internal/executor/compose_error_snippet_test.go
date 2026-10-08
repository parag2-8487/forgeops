// SPDX-License-Identifier: Apache-2.0

package executor

import (
	"strings"
	"testing"
)

// The error text handed to the AI resolver decides whether the repair can succeed. These cases are
// real log shapes: the first is the failure that reached a user as "npm run build ... exit code: 127",
// and it must arrive with the line that names the actual cause.
func TestComposeErrorSnippetKeepsTheCause(t *testing.T) {
	cases := []struct {
		name   string
		output string
		want   string
	}{
		{
			name: "typescript_compile_failure",
			output: `#10 [builder 4/6] RUN npm install
#10 CACHED
#11 [builder 6/6] RUN npm run build
#11 0.894 src/main.tsx(1,28): error TS7016: Could not find a declaration file for module 'react'.
#11 1.115 error TS2688: Cannot find type definition file for 'node'.
#11 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1
------
 > [builder 6/6] RUN npm run build:
------
failed to solve: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1`,
			want: "error TS7016",
		},
		{
			name: "build_tool_stripped_by_production_install",
			output: `#8 [builder 3/5] RUN npm install --production
#9 [builder 4/5] RUN npm run build
#9 0.201 sh: 1: tsc: not found
#9 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 127`,
			want: "tsc: not found",
		},
		{
			name:   "go_build_failure",
			output: "Step 5/9 : RUN go build -o /app/server .\n ---> Running in abc\n./main.go:3:2: undefined: htttp\nERROR: failed to solve: process failed",
			want:   "undefined: htttp",
		},
		{
			name:   "copy_path_missing",
			output: "#7 [builder 2/4] COPY package*.json ./\n#7 ERROR: failed to compute cache key: \"/package-lock.json\" not found",
			want:   "failed to compute cache key",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := composeErrorSnippet(tc.output)
			if !strings.Contains(got, tc.want) {
				t.Errorf("the explanation never reached the resolver; want %q in:\n%s", tc.want, got)
			}
		})
	}
}

// Progress lines carry no diagnostic value and, at one line per build step, they crowd the real cause
// out of a bounded snippet.
func TestComposeErrorSnippetDropsProgressNoise(t *testing.T) {
	noise := strings.Join([]string{
		"#1 [internal] load build definition from Dockerfile",
		"#1 DONE 0.0s",
		"#2 [internal] load metadata for docker.io/library/node:22-slim",
		"#2 DONE 0.6s",
		"#3 [builder 1/6] FROM docker.io/library/node:22-slim",
		"#3 CACHED",
	}, "\n")

	got := composeErrorSnippet(noise)
	if strings.Contains(got, "CACHED") || strings.Contains(got, "#2 DONE") {
		t.Errorf("progress noise survived into the snippet shown to the operator:\n%s", got)
	}
}

func TestComposeErrorSnippetHandlesEmptyOutput(t *testing.T) {
	got := composeErrorSnippet("")
	if strings.TrimSpace(got) == "" {
		t.Errorf("an empty log produced an empty snippet; the operator would see nothing")
	}
}
