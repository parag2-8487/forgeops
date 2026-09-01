// SPDX-License-Identifier: Apache-2.0
package validator

import "testing"

// TestTofuDiagnostics_SurvivesCIWorkflowAnnotations is a regression test for a CI-only failure.
//
// Under GitHub Actions, OpenTofu detects the environment and appends workflow commands after its own JSON:
//
//	}
//	::error::OpenTofu exited with code 1.
//
// The document is then unparseable, `tofuDiagnostics` takes its line-splitting fallback, and every LINE of
// the JSON becomes a separate HIGH finding carrying the module directory instead of the `.tf` file and line
// the diagnostic names. `validate.tofu`'s whole value is saying which line of which file is wrong, so this
// is a silent loss of the answer rather than a formatting difference.
//
// It passed locally and failed only in CI, which is the shape that makes a bug expensive: the annotation is
// emitted nowhere else. The fixture below is the literal output from the failing run rather than a
// reconstruction.
func TestTofuDiagnostics_SurvivesCIWorkflowAnnotations(t *testing.T) {
	raw := "{\n" +
		"  \"format_version\": \"1.0\",\n" +
		"  \"valid\": false,\n" +
		"  \"error_count\": 1,\n" +
		"  \"diagnostics\": [\n" +
		"    {\n" +
		"      \"severity\": \"error\",\n" +
		"      \"summary\": \"Reference to undeclared input variable\",\n" +
		"      \"detail\": \"An input variable with the name \\\"undeclared_everywhere\\\" has not been declared.\",\n" +
		"      \"range\": {\"filename\": \"main.tf\", \"start\": {\"line\": 3, \"column\": 11}}\n" +
		"    }\n" +
		"  ]\n" +
		"}\n" +
		"::error::OpenTofu exited with code 1.\n"

	findings := tofuDiagnostics(raw, "/tmp/module")
	if len(findings) != 1 {
		t.Fatalf("got %d finding(s), want exactly 1 — more means the line-splitting fallback ran: %+v",
			len(findings), findings)
	}
	got := findings[0]
	if got.Path != "main.tf" {
		t.Errorf("path %q, want main.tf; the module directory means the range was not read", got.Path)
	}
	if got.Line != 3 {
		t.Errorf("line %d, want 3", got.Line)
	}
	if got.Severity != SeverityHigh {
		t.Errorf("severity %q, want high", got.Severity)
	}
	// Summary and detail are both reported: the summary alone does not say what to change.
	if got.Message == "" || got.Message == "Reference to undeclared input variable" {
		t.Errorf("message %q does not carry the detail", got.Message)
	}
}

func TestJsonObject_ExtractsTheOutermostObject(t *testing.T) {
	cases := map[string]string{
		`{"a":1}`:                       `{"a":1}`,
		"noise\n{\"a\":1}\ntrailing":    `{"a":1}`,
		"{\"a\":{\"b\":2}}\n::error::x": `{"a":{"b":2}}`,
		// No object at all: returned unchanged, so the line-splitting fallback still reports something.
		// A crash or a version that stops honouring `-json` must not produce silence.
		"tofu: command failed": "tofu: command failed",
		"":                     "",
		// A closing brace before an opening one is not an object.
		"} {": "} {",
	}
	for input, want := range cases {
		if got := jsonObject(input); got != want {
			t.Errorf("jsonObject(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestTofuDiagnostics_FallsBackWhenThereIsNoJson(t *testing.T) {
	// The fallback is deliberate: reporting nothing would be worse than reporting lines.
	findings := tofuDiagnostics("Error: could not load plugin\nsomething else\n", "/tmp/module")
	if len(findings) == 0 {
		t.Fatal("non-JSON output produced no findings, so a tool crash would be reported as success")
	}
	for _, finding := range findings {
		if finding.Path != "/tmp/module" {
			t.Errorf("fallback finding path %q, want the module directory", finding.Path)
		}
	}
}
