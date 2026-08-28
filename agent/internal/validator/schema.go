// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v5"
	"gopkg.in/yaml.v3"
)

// Schema is a compiled JSON Schema applied to a YAML or JSON document.
//
// WHY A SCHEMA AND NOT A KEY LIST. The predecessor was `ValidateYAMLOrJSON(content string,
// requiredKeys []string)`, which checked that some top-level keys were present. That accepts a
// GitHub Actions workflow whose `jobs` is a string, whose `runs-on` sits at the top level, or whose
// `steps` is a map — all of which parse, satisfy a key list, and cannot run. FR-24 requires
// generating Actions workflows, Helm charts and OpenTofu configs, and a generated workflow that
// GitHub will reject is exactly what validation is supposed to stop reaching a user.
//
// Draft 2020-12 via `santhosh-tekuri/jsonschema`, which reports an instance location per error, so a
// finding can name the path inside the document rather than the file as a whole.
type Schema struct {
	Name     string
	compiled *jsonschema.Schema
}

// CompileSchema builds a Schema from a JSON Schema document.
func CompileSchema(name string, document []byte) (*Schema, error) {
	compiler := jsonschema.NewCompiler()
	compiler.Draft = jsonschema.Draft2020
	// Parsed first so a malformed schema is reported as such, rather than as an obscure compile
	// error from deep inside the library.
	var parsed any
	if err := json.Unmarshal(document, &parsed); err != nil {
		return nil, fmt.Errorf("validator: schema %q is not valid JSON: %w", name, err)
	}
	resource := name + ".schema.json"
	if err := compiler.AddResource(resource, bytes.NewReader(document)); err != nil {
		return nil, fmt.Errorf("validator: schema %q could not be added: %w", name, err)
	}
	compiled, err := compiler.Compile(resource)
	if err != nil {
		return nil, fmt.Errorf("validator: schema %q could not be compiled: %w", name, err)
	}
	return &Schema{Name: name, compiled: compiled}, nil
}

// Check validates one file against the schema and returns a finding per violation.
//
// An `error` return means the file could not be read or parsed as YAML — a different fact from "the
// document is the wrong shape", and one the caller must not report as a schema violation.
func (s *Schema) Check(path string) ([]Finding, error) {
	raw, err := os.ReadFile(path) //nolint:gosec // path is confined by fileops.ResolveForRead upstream
	if err != nil {
		return nil, fmt.Errorf("validator: cannot read %s: %w", path, err)
	}
	var document any
	if err := yaml.Unmarshal(raw, &document); err != nil {
		return nil, fmt.Errorf("validator: %s is not parsable YAML: %w", path, err)
	}
	// yaml.v3 yields map[string]any for mappings, which the schema library accepts directly. JSON
	// round-tripping normalises the remaining scalar types (dates, unquoted numerics) so that a
	// value the schema calls a string is compared as one.
	normalised, err := normaliseForSchema(document)
	if err != nil {
		return nil, fmt.Errorf("validator: %s could not be normalised: %w", path, err)
	}

	if err := s.compiled.Validate(normalised); err != nil {
		var ve *jsonschema.ValidationError
		if !errorsAs(err, &ve) {
			return []Finding{{
				Severity: SeverityHigh, Rule: "schema:" + s.Name,
				Message: err.Error(), Path: path,
			}}, nil
		}
		return schemaFindings(ve, s.Name, path), nil
	}
	return nil, nil
}

func normaliseForSchema(document any) (any, error) {
	encoded, err := json.Marshal(document)
	if err != nil {
		return nil, err
	}
	var normalised any
	if err := json.Unmarshal(encoded, &normalised); err != nil {
		return nil, err
	}
	return normalised, nil
}

// schemaFindings flattens the validation error tree into leaf causes.
//
// Only the leaves, and deduplicated. The library reports a chain from the root down to each actual
// cause; emitting every level would turn one wrong field into six findings that all say the same
// thing at different depths, which makes a report look worse than the artifact is.
func schemaFindings(ve *jsonschema.ValidationError, schemaName, path string) []Finding {
	seen := map[string]Finding{}
	var walk func(node *jsonschema.ValidationError)
	walk = func(node *jsonschema.ValidationError) {
		if len(node.Causes) == 0 {
			location := node.InstanceLocation
			if location == "" {
				location = "(document root)"
			}
			message := fmt.Sprintf("%s: %s", location, node.Message)
			seen[message] = Finding{
				Severity: SeverityHigh,
				Rule:     "schema:" + schemaName,
				Message:  message,
				Path:     path,
			}
			return
		}
		for _, cause := range node.Causes {
			walk(cause)
		}
	}
	walk(ve)

	keys := make([]string, 0, len(seen))
	for key := range seen {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	findings := make([]Finding, 0, len(keys))
	for _, key := range keys {
		findings = append(findings, seen[key])
	}
	return findings
}

// errorsAs is a tiny indirection so this file does not import `errors` solely for one call, keeping
// the import list honest about what the package depends on.
func errorsAs(err error, target **jsonschema.ValidationError) bool {
	for err != nil {
		if ve, ok := err.(*jsonschema.ValidationError); ok {
			*target = ve
			return true
		}
		unwrapper, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = unwrapper.Unwrap()
	}
	return false
}

// checkAllDocuments applies a schema to every document in a possibly multi-document YAML file.
//
// A Kubernetes manifest is routinely several objects separated by `---`, and `Check` reads only the
// first. Validating document one and reporting the file as valid is the kind of partial check that
// reads as complete, so every document is checked and an empty file is itself a finding.
func checkAllDocuments(path string, schema *Schema) ([]Finding, error) {
	raw, err := os.ReadFile(path) //nolint:gosec // confined by fileops.ResolveForRead upstream
	if err != nil {
		return nil, fmt.Errorf("validator: cannot read %s: %w", path, err)
	}
	decoder := yaml.NewDecoder(bytes.NewReader(raw))
	var findings []Finding
	documents := 0
	for {
		var document any
		decodeErr := decoder.Decode(&document)
		if decodeErr != nil {
			if errors.Is(decodeErr, io.EOF) {
				break
			}
			return nil, fmt.Errorf("validator: %s is not parsable YAML: %w", path, decodeErr)
		}
		if document == nil {
			// A `---` separator with nothing after it. Not an object, and not an error either.
			continue
		}
		documents++
		normalised, normErr := normaliseForSchema(document)
		if normErr != nil {
			return nil, fmt.Errorf("validator: %s could not be normalised: %w", path, normErr)
		}
		if validateErr := schema.compiled.Validate(normalised); validateErr != nil {
			var ve *jsonschema.ValidationError
			if errorsAs(validateErr, &ve) {
				for _, finding := range schemaFindings(ve, schema.Name, path) {
					finding.Message = fmt.Sprintf("document %d: %s", documents, finding.Message)
					findings = append(findings, finding)
				}
				continue
			}
			findings = append(findings, Finding{
				Severity: SeverityHigh, Rule: "schema:" + schema.Name,
				Message: fmt.Sprintf("document %d: %s", documents, validateErr.Error()), Path: path,
			})
		}
	}
	if documents == 0 {
		findings = append(findings, Finding{
			Severity: SeverityHigh, Rule: "schema:" + schema.Name, Path: path,
			Message: "the file contains no YAML document, so it declares no object",
		})
	}
	return findings, nil
}

// SchemaFor returns the schema that applies to a path, or nil when none does.
//
// Chosen by location rather than by content: a file under `.github/workflows/` is a workflow whether
// or not it happens to contain the keys one has, and that is precisely the case where a schema check
// is worth having. Returning nil is a valid answer — most YAML in a repository has no schema, and
// yamllint alone is the honest level of assurance for it.
func SchemaFor(relPath string) *Schema {
	normalised := strings.ReplaceAll(relPath, "\\", "/")
	switch {
	case strings.Contains(normalised, ".github/workflows/"):
		return githubWorkflowSchema
	case strings.HasSuffix(normalised, "/Chart.yaml") || normalised == "Chart.yaml":
		return helmChartSchema
	default:
		return nil
	}
}
