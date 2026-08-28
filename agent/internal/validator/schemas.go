// SPDX-License-Identifier: Apache-2.0
package validator

// The schemas applied to the artifact kinds FR-24 requires generating.
//
// Written here rather than fetched, for the reason §7.7 pins every other tool: a validator that
// downloads its own schema at run time is a network dependency on a path that must work offline, and
// a schema that can change under the agent means the same artifact validates differently on two
// days. These are narrow on purpose — they assert the structure that makes the file *runnable*, not
// every optional field GitHub or Helm accepts. A schema that rejects a valid file is worse than one
// that accepts an unusual one, because the first blocks correct work.

var githubWorkflowSchema = mustCompile("github-workflow", []byte(`{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "GitHub Actions workflow",
  "type": "object",
  "required": ["on", "jobs"],
  "properties": {
    "name": { "type": "string" },
    "on": {
      "description": "A trigger, a list of triggers, or a mapping of trigger to configuration.",
      "anyOf": [
        { "type": "string" },
        { "type": "array", "items": { "type": "string" }, "minItems": 1 },
        { "type": "object", "minProperties": 1 }
      ]
    },
    "permissions": {
      "anyOf": [
        { "type": "string", "enum": ["read-all", "write-all"] },
        { "type": "object" }
      ]
    },
    "env": { "type": "object" },
    "concurrency": { "anyOf": [{ "type": "string" }, { "type": "object" }] },
    "jobs": {
      "type": "object",
      "minProperties": 1,
      "additionalProperties": {
        "type": "object",
        "anyOf": [
          { "required": ["runs-on"] },
          { "required": ["uses"] }
        ],
        "properties": {
          "runs-on": {
            "anyOf": [
              { "type": "string" },
              { "type": "array", "items": { "type": "string" }, "minItems": 1 },
              { "type": "object" }
            ]
          },
          "uses": { "type": "string" },
          "needs": {
            "anyOf": [
              { "type": "string" },
              { "type": "array", "items": { "type": "string" } }
            ]
          },
          "if": { "anyOf": [{ "type": "string" }, { "type": "boolean" }] },
          "permissions": { "anyOf": [{ "type": "string" }, { "type": "object" }] },
          "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
              "type": "object",
              "anyOf": [
                { "required": ["uses"] },
                { "required": ["run"] }
              ],
              "properties": {
                "name": { "type": "string" },
                "uses": { "type": "string" },
                "run": { "type": "string" },
                "with": { "type": "object" },
                "env": { "type": "object" },
                "if": { "anyOf": [{ "type": "string" }, { "type": "boolean" }] },
                "shell": { "type": "string" },
                "working-directory": { "type": "string" }
              },
              "additionalProperties": true
            }
          }
        },
        "additionalProperties": true
      }
    }
  },
  "additionalProperties": true
}`))

var helmChartSchema = mustCompile("helm-chart", []byte(`{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Helm Chart.yaml",
  "type": "object",
  "required": ["apiVersion", "name", "version"],
  "properties": {
    "apiVersion": { "type": "string", "enum": ["v1", "v2"] },
    "name": { "type": "string", "minLength": 1 },
    "version": {
      "description": "SemVer 2, which Helm requires rather than prefers.",
      "type": "string",
      "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?(\\+[0-9A-Za-z.-]+)?$"
    },
    "appVersion": { "anyOf": [{ "type": "string" }, { "type": "number" }] },
    "description": { "type": "string" },
    "type": { "type": "string", "enum": ["application", "library"] },
    "kubeVersion": { "type": "string" },
    "dependencies": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["name", "version"],
        "properties": {
          "name": { "type": "string" },
          "version": { "type": "string" },
          "repository": { "type": "string" }
        },
        "additionalProperties": true
      }
    }
  },
  "additionalProperties": true
}`))

// k8sManifestSchema is the offline fallback for `validate.k8s`.
//
// WHY THERE IS A FALLBACK AT ALL. `kubectl apply --dry-run=client` cannot work without a cluster: it
// needs a RESTMapper, so it performs API discovery before it will look at a file, and with no
// reachable server it fails with `couldn't get current server API group list` — a network error that
// says nothing about the manifest. Even `--validate=false` does not avoid it. So on a developer
// machine with no cluster, and in CI, the choice is between checking the manifest locally and not
// checking it.
//
// This is deliberately NOT a full Kubernetes schema. Shipping the OpenAPI for every built-in kind
// plus whatever CRDs a cluster has is both enormous and wrong — the CRDs are the cluster's, not the
// binary's. What it asserts is the structure every Kubernetes object must have whatever its kind:
// a non-empty `apiVersion`, a non-empty `kind`, and `metadata.name` present and non-empty. That
// catches an empty document, a missing kind, a manifest with no name, and a `metadata` that is a
// string — all of which a cluster rejects and all of which used to sail through.
//
// The mode string says plainly that kind-specific validation did not run, so nobody reads this as
// more than it is.
var k8sManifestSchema = mustCompile("k8s-manifest", []byte(`{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Kubernetes object (kind-independent structure)",
  "type": "object",
  "required": ["apiVersion", "kind", "metadata"],
  "properties": {
    "apiVersion": { "type": "string", "minLength": 1 },
    "kind": { "type": "string", "minLength": 1 },
    "metadata": {
      "type": "object",
      "required": ["name"],
      "properties": {
        "name": { "type": "string", "minLength": 1 },
        "namespace": { "type": "string" },
        "labels": { "type": "object" },
        "annotations": { "type": "object" }
      },
      "additionalProperties": true
    }
  },
  "additionalProperties": true
}`))

// mustCompile panics on a broken built-in schema.
//
// A panic at package initialisation is correct here and is not the same as panicking on bad input:
// these two documents are compiled into the binary, so a failure means the binary itself is broken
// and every validation it performs would be meaningless. `TestBuiltInSchemasCompile` covers it so
// the failure arrives in a test run rather than on an operator's machine.
func mustCompile(name string, document []byte) *Schema {
	schema, err := CompileSchema(name, document)
	if err != nil {
		panic("validator: built-in schema " + name + " is broken: " + err.Error())
	}
	return schema
}
