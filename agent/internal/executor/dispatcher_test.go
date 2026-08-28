// SPDX-License-Identifier: Apache-2.0

// The dispatch table is the whole authorisation surface for an operation, so these tests are
// mostly about the table rather than about any handler: §7.7's catalogue is closed, every member
// has exactly one way in, and the mutating half cannot be reached without an approval (D-83).
package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/envelope"
	"github.com/parag8487/ForgeOps/agent/internal/session"
)

const testKeyUTF8 = "test-only-not-a-real-secret-executor-key"

// verified builds a genuine *envelope.Verified by signing and verifying a real envelope.
//
// No shortcut and no test-only constructor: the type's whole value is that only
// `envelope.Verify` can produce one, and a seam here would be a second constructor. Which also
// means these tests exercise the same path production takes.
func verified(t *testing.T, op Operation, approvalID string, args any, seq int64) *envelope.Verified {
	t.Helper()
	const deviceID = "dev-executor-0001"
	const digest = "sha256:0101010101010101010101010101010101010101010101010101010101010101"

	encoded, err := json.Marshal(args)
	if err != nil {
		t.Fatalf("marshalling args: %v", err)
	}
	now := time.Unix(1899999900, 0).UTC()
	env := envelope.Envelope{
		V:             envelope.Version,
		CommandID:     fmt.Sprintf("cmd-%d", seq),
		DeviceID:      deviceID,
		Operation:     envelope.Operation(op),
		Args:          encoded,
		ApprovalID:    approvalID,
		PolicyContext: envelope.PolicyContext{BundleDigest: digest, Decision: "allow"},
		Nonce:         fmt.Sprintf("%032x", seq),
		Seq:           seq,
		NotAfter:      now.Add(60 * time.Second).Unix(),
	}
	signature, err := envelope.Sign(envelope.DomainPrefix, env, []byte(testKeyUTF8))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	env.Signature = signature
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}

	keys := envelope.NewStaticKeySource()
	keys.Set(deviceID, []byte(testKeyUTF8))
	guard, err := envelope.NewMemoryReplayGuard(300*time.Second, 64)
	if err != nil {
		t.Fatalf("NewMemoryReplayGuard: %v", err)
	}
	verifier, err := envelope.NewVerifier(keys, guard, envelope.NewStaticBundleDigest(digest),
		envelope.WithClock(func() time.Time { return now }))
	if err != nil {
		t.Fatalf("NewVerifier: %v", err)
	}
	result, err := verifier.Verify(context.Background(), raw)
	if err != nil {
		t.Fatalf("the test envelope must verify: %v", err)
	}
	return result
}

func newDispatcher(t *testing.T, root string) Dispatcher {
	t.Helper()
	d, err := New(Deps{Root: root})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return d
}

// recordingSink captures progress so "emits progress" is a measurement.
type recordingSink struct {
	events []string
}

func (r *recordingSink) Progress(percent int, stage, message string) {
	r.events = append(r.events, fmt.Sprintf("%d/%s/%s", percent, stage, message))
}

// ── The catalogue is closed, and the table is the catalogue ────────────────────────────────

func TestEveryDeclaredOperationHasAHandlerAndViceVersa(t *testing.T) {
	// Both directions. One direction alone leaves the other hole open: a constant with no entry
	// is an operation the backend can send and the agent panics on or refuses as unknown, and an
	// entry with no constant is an operation nothing in the codebase names.
	for _, op := range allOperations {
		row, ok := handlerTable[op]
		if !ok {
			t.Errorf("operation %q is declared and has no handler", op)
			continue
		}
		if row.run == nil {
			t.Errorf("operation %q has a nil handler, which would panic on first use", op)
		}
		if row.timeout <= 0 {
			t.Errorf("operation %q has no timeout; §10.5 requires a per-operation bound", op)
		}
	}
	declared := map[Operation]bool{}
	for _, op := range allOperations {
		declared[op] = true
	}
	for op := range handlerTable {
		if !declared[op] {
			t.Errorf("the table carries %q, which is not a declared Operation constant", op)
		}
	}
	if len(handlerTable) != len(allOperations) {
		t.Errorf("table has %d rows, %d operations declared", len(handlerTable), len(allOperations))
	}
	if len(allOperations) != 17 {
		t.Errorf("§7.7's catalogue has 17 operations; this build declares %d. If that is "+
			"deliberate, change this number in the same commit as the table.", len(allOperations))
	}
}

func TestTheMutatingSetIsExactlySevenSevensSecondColumn(t *testing.T) {
	// §7.7's second column, written out, so a new operation cannot join the mutating half by
	// accident and a mutating one cannot quietly become non-mutating.
	want := map[Operation]bool{
		OpChangeSetApply:      true,
		OpChangeSetRevert:     true,
		OpGitBranchCommitPush: true,
		OpGitOpenPR:           true,
		OpSecretsInject:       true,
	}
	for op, row := range handlerTable {
		if row.mutating != want[op] {
			t.Errorf("%q: mutating = %v, §7.7 says %v", op, row.mutating, want[op])
		}
	}
}

func TestEveryMutatingOperationRequiresAnApproval(t *testing.T) {
	// D-83's strengthening. The blanket `approval_id` check in `envelope.parse` is gone, so this
	// is the assertion that nothing mutating slipped through the change: the requirement is now
	// per operation, and every mutating operation must carry it.
	for op, row := range handlerTable {
		if row.mutating && !row.requiresApproval {
			t.Errorf("%q mutates and does not require an approval_id", op)
		}
		if row.requiresApproval && !row.mutating {
			t.Errorf("%q requires an approval_id and does not mutate; §7.7 pairs the two columns", op)
		}
	}
}

func TestTheCatalogueContainsNoShellShapedOperation(t *testing.T) {
	// `phases.md` §1.1: named operations, never arbitrary shell. Checked by name AND by
	// argument shape, because "no operation takes a command string" is the half a name check
	// misses — `project.register{command: "..."}` would pass a name-only test.
	for _, op := range allOperations {
		lower := strings.ToLower(string(op))
		for _, banned := range []string{"exec", "shell", "run_command", "runcommand", "eval", "spawn"} {
			if strings.Contains(lower, banned) {
				t.Errorf("operation %q contains %q; the catalogue admits no arbitrary execution", op, banned)
			}
		}
	}
	for _, field := range argumentFieldNames(t) {
		switch field {
		case "command", "cmd", "script", "shell", "args_string", "commandline", "command_line":
			t.Errorf("an argument struct declares a %q field; no operation may take a command string", field)
		}
	}
}

// argumentFieldNames returns every JSON member name declared by a struct in this package.
func argumentFieldNames(t *testing.T) []string {
	t.Helper()
	names := []string{}
	for _, file := range packageFiles(t) {
		ast.Inspect(file, func(node ast.Node) bool {
			structType, ok := node.(*ast.StructType)
			if !ok {
				return true
			}
			for _, field := range structType.Fields.List {
				if field.Tag == nil {
					continue
				}
				tag := strings.Trim(field.Tag.Value, "`")
				if index := strings.Index(tag, `json:"`); index >= 0 {
					rest := tag[index+len(`json:"`):]
					if end := strings.Index(rest, `"`); end >= 0 {
						member := strings.Split(rest[:end], ",")[0]
						names = append(names, strings.ToLower(member))
					}
				}
			}
			return true
		})
	}
	if len(names) == 0 {
		t.Fatal("no JSON members found in this package; the parse is broken and this test proves nothing")
	}
	return names
}

// packageFiles parses this package's non-test sources.
func packageFiles(t *testing.T) []*ast.File {
	t.Helper()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	fset := token.NewFileSet()
	files := []*ast.File{}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		parsed, err := parser.ParseFile(fset, filepath.Join(".", name), nil, parser.ParseComments)
		if err != nil {
			t.Fatalf("parsing %s: %v", name, err)
		}
		files = append(files, parsed)
	}
	if len(files) == 0 {
		t.Fatal("no source files parsed")
	}
	return files
}

// TestNoHandlerIsReachableOutsideTheTable is §10.5's "the ONLY dispatch surface", as a check.
//
// Each handler function must be referenced exactly once outside its own declaration, and that
// reference must be inside `handlerTable`. A second call site would be a second way in — a
// convenience wrapper, a retry helper, an "internal" fast path — and it would skip the approval
// requirement and the per-operation timeout, which live in Execute rather than in the handlers.
func TestNoHandlerIsReachableOutsideTheTable(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	fset := token.NewFileSet()
	var files []*ast.File
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		parsed, err := parser.ParseFile(fset, filepath.Join(".", name), nil, 0)
		if err != nil {
			t.Fatalf("parsing %s: %v", name, err)
		}
		files = append(files, parsed)
	}

	// The handlers are the top-level funcs whose signature ends in (Result, error) and which
	// take a *envelope.Verified. Derived from the source rather than listed, so a new handler is
	// covered the day it is written.
	//
	// The declaration POSITION is recorded, not just the name. The previous version asked
	// `ident.Obj.Decl` whether an identifier was its own declaration, and `ast.Object` resolution
	// is per FILE — so the moment a handler lived in any file other than the one holding
	// `handlerTable`, `Obj` was nil and this test panicked on the nil dereference instead of
	// checking anything. It reads every non-test file in the package, so package-wide was always
	// the intent; comparing positions is what makes that true. The property asserted is
	// unchanged, and a nil `Obj` can no longer be mistaken for "this is the declaration".
	handlers := map[string]bool{}
	handlerDecl := map[string]token.Pos{}
	var tableStart, tableEnd token.Pos
	for _, file := range files {
		for _, decl := range file.Decls {
			switch typed := decl.(type) {
			case *ast.FuncDecl:
				if typed.Recv == nil && isHandlerSignature(typed.Type) {
					handlers[typed.Name.Name] = true
					handlerDecl[typed.Name.Name] = typed.Name.Pos()
				}
			case *ast.GenDecl:
				for _, spec := range typed.Specs {
					value, ok := spec.(*ast.ValueSpec)
					if !ok {
						continue
					}
					for _, name := range value.Names {
						if name.Name == "handlerTable" {
							tableStart, tableEnd = value.Pos(), value.End()
						}
					}
				}
			}
		}
	}
	if len(handlers) == 0 {
		t.Fatal("no handler functions found; the derivation is broken and this test proves nothing")
	}
	if tableStart == token.NoPos {
		t.Fatal("handlerTable was not found; §10.5's single dispatch surface is gone")
	}

	for _, file := range files {
		ast.Inspect(file, func(node ast.Node) bool {
			ident, ok := node.(*ast.Ident)
			if !ok || !handlers[ident.Name] {
				return true
			}
			// Its own declaration is fine.
			if handlerDecl[ident.Name] == ident.Pos() {
				return true
			}
			if ident.Pos() >= tableStart && ident.Pos() <= tableEnd {
				return true
			}
			t.Errorf("handler %q is referenced at %s, outside handlerTable; the table must be the "+
				"only dispatch surface (§10.5)", ident.Name, fset.Position(ident.Pos()))
			return true
		})
	}
}

func isHandlerSignature(fn *ast.FuncType) bool {
	if fn.Results == nil || len(fn.Results.List) != 2 {
		return false
	}
	first, ok := fn.Results.List[0].Type.(*ast.Ident)
	if !ok || first.Name != "Result" {
		return false
	}
	for _, param := range fn.Params.List {
		star, ok := param.Type.(*ast.StarExpr)
		if !ok {
			continue
		}
		selector, ok := star.X.(*ast.SelectorExpr)
		if ok && selector.Sel.Name == "Verified" {
			return true
		}
	}
	return false
}

func TestTheHandlerDerivationWouldNoticeASecondCallSite(t *testing.T) {
	// The control for the clause above: a parse that found nothing would pass it silently. This
	// asserts the derivation actually sees the handlers that exist.
	found := 0
	for _, file := range packageFiles(t) {
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if ok && fn.Recv == nil && isHandlerSignature(fn.Type) {
				found++
			}
		}
	}
	if found < 2 {
		t.Errorf("the derivation found %d handler(s); applyChangeSet and revertChangeSet exist, so "+
			"the signature matcher is broken", found)
	}
}

// ── Execute's guards ───────────────────────────────────────────────────────────────────────

func TestExecute_RefusesANilVerified(t *testing.T) {
	d := newDispatcher(t, t.TempDir())
	if _, err := d.Execute(context.Background(), nil, nil); Code(err) == "" {
		t.Fatalf("a nil envelope was accepted: %v", err)
	} else if !isErr(err, ErrNoAuthority) {
		t.Fatalf("err = %v, want ErrNoAuthority", err)
	}
}

func TestExecute_AMutatingOperationWithNoApprovalIsRefusedAndWritesNothing(t *testing.T) {
	// D-83's replacement rule, and the clause that makes the relaxation in `envelope.parse` a
	// move rather than a loss.
	root := t.TempDir()
	d := newDispatcher(t, root)
	args := applyArgs{Entries: []applyEntry{{Path: "a.txt", Action: "create", Content: "hello"}}}

	_, err := d.Execute(context.Background(), verified(t, OpChangeSetApply, "", args, 1), nil)
	if !isErr(err, ErrApprovalRequired) {
		t.Fatalf("err = %v, want ErrApprovalRequired", err)
	}
	if Code(err) != "approval-required" {
		t.Errorf("code = %q, want approval-required", Code(err))
	}
	if _, statErr := os.Stat(filepath.Join(root, "a.txt")); statErr == nil {
		t.Error("the refused apply wrote the file anyway")
	}
}

func TestExecute_TheControlShowsTheSameApplySucceedsWithAnApproval(t *testing.T) {
	// Without this, the clause above would pass for a dispatcher that refused everything.
	root := t.TempDir()
	d := newDispatcher(t, root)
	sink := &recordingSink{}
	args := applyArgs{Entries: []applyEntry{{Path: "a.txt", Action: "create", Content: "hello"}}}

	result, err := d.Execute(context.Background(), verified(t, OpChangeSetApply, "approval-1", args, 2), sink)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if result.Status != "succeeded" {
		t.Errorf("status = %q", result.Status)
	}
	content, err := os.ReadFile(filepath.Join(root, "a.txt"))
	if err != nil || string(content) != "hello" {
		t.Errorf("file content = %q, err = %v", content, err)
	}
	if len(result.BackupManifest) == 0 {
		t.Error("no backup manifest returned; the backend has no rollback handle to persist")
	}
	if result.Hashes["a.txt"] == "" {
		t.Error("no post-image hash for the written file")
	}
	if len(sink.events) < 2 {
		t.Errorf("progress events = %v; §10.5 requires progress emission", sink.events)
	}
}

func TestExecute_AnUnknownOperationIsDistinctFromAnUnimplementedOne(t *testing.T) {
	// The two facts a backend has to tell apart: "we do not have that operation" is a version
	// skew, "we have it and its body arrives later" is a decomposition boundary (D-85). One code
	// for both would send an operator hunting the wrong problem.
	d := newDispatcher(t, t.TempDir())

	_, unknown := d.Execute(context.Background(),
		verified(t, Operation("files.delete_everything"), "approval-1", map[string]any{}, 3), nil)
	if !isErr(unknown, ErrUnknownOperation) || Code(unknown) != "operation-unknown" {
		t.Errorf("an off-catalogue operation gave %v (code %q)", unknown, Code(unknown))
	}

	// `project.register`, not `validate.compose` and not `scan.full`. This assertion needs a row that
	// is genuinely catalogued-but-absent, and the example has had to move twice for exactly the
	// reason the original comment gave: the scan operations became implemented when the indexer
	// landed, and the six validators became implemented when they were built against real tools.
	// An example that quietly stops being an example of the thing it illustrates is how this
	// assertion would go vacuous, so `TestTheUnimplementedExampleIsStillUnimplemented` below pins
	// the choice rather than leaving it to be noticed.
	_, unimplemented := d.Execute(context.Background(),
		verified(t, OpProjectRegister, "", map[string]any{}, 4), nil)
	if !isErr(unimplemented, ErrUnimplemented) || Code(unimplemented) != "operation-unimplemented" {
		t.Errorf("a catalogued-but-unimplemented operation gave %v (code %q)",
			unimplemented, Code(unimplemented))
	}
	if Code(unknown) == Code(unimplemented) {
		t.Error("the two report the same code, so the distinction is unobservable")
	}
}

// TestTheUnimplementedExampleIsStillUnimplemented keeps the assertion above from going vacuous.
//
// The test it guards needs one catalogued operation with no body. When the last such row gains one,
// that test can no longer demonstrate anything and must be deleted rather than pointed at an
// implemented operation — which would make it assert the opposite of its name while still passing.
// This states the dependency so the failure names the reason.
func TestTheUnimplementedExampleIsStillUnimplemented(t *testing.T) {
	row, ok := handlerTable[OpProjectRegister]
	if !ok {
		t.Fatal("project.register left the catalogue")
	}
	if row.implemented {
		t.Fatal("project.register is now implemented, so " +
			"TestExecute_AnUnknownOperationIsDistinctFromAnUnimplementedOne needs a different " +
			"example or, if none remains, deletion")
	}
}

func TestExecute_AReadOnlyOperationNeedsNoApprovalToReachItsHandler(t *testing.T) {
	// §7.7's third column, and the whole reason D-83 moved the check. An empty `approval_id` on
	// `scan.full` must reach the handler — which then refuses for its own reason — rather than be
	// refused as malformed.
	//
	// The handler's own reason is now `ErrNoIndexer` rather than `ErrUnimplemented`, because
	// `scan.full` has a body and `newDispatcher` wires no `CodebaseIndexer`. The property under
	// test is unchanged and still the same one: what matters is that an operation §7.7 marks
	// read-only got past the approval gate and into its body.
	d := newDispatcher(t, t.TempDir())
	_, err := d.Execute(context.Background(), verified(t, OpScanFull, "", map[string]any{}, 5), nil)
	if isErr(err, ErrApprovalRequired) {
		t.Fatal("a read-only operation was refused for having no approval_id")
	}
	if !isErr(err, ErrNoIndexer) {
		t.Fatalf("err = %v, want the handler's own ErrNoIndexer", err)
	}
}

func TestExecute_MalformedArgsAreRefusedBeforeAnyWrite(t *testing.T) {
	root := t.TempDir()
	d := newDispatcher(t, root)
	for _, tc := range []struct {
		name string
		args any
	}{
		{name: "no entries", args: applyArgs{}},
		{name: "an unknown action", args: applyArgs{Entries: []applyEntry{{Path: "a.txt", Action: "chmod"}}}},
		{
			// `args` that is not an object at all cannot even be signed — `envelope`'s
			// canonicaliser refuses it (§7.7's operations all take an object), which is the
			// right layer for that refusal. This is the shape that IS an object and still
			// wrong.
			name: "entries that are not a list",
			args: map[string]any{"entries": "a.txt"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := d.Execute(context.Background(),
				verified(t, OpChangeSetApply, "approval-1", tc.args, 6), nil)
			if !isErr(err, ErrBadArgs) {
				t.Fatalf("err = %v, want ErrBadArgs", err)
			}
		})
	}
	files, err := os.ReadDir(root)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	if len(files) != 0 {
		t.Errorf("the workspace holds %d entr(ies) after only refusals", len(files))
	}
}

func TestExecute_ApplyThenRevertRoundTrips(t *testing.T) {
	root := t.TempDir()
	existing := filepath.Join(root, "kept.txt")
	if err := os.WriteFile(existing, []byte("original"), 0o644); err != nil {
		t.Fatalf("WriteFile: %v", err)
	}
	d := newDispatcher(t, root)

	create := applyArgs{Entries: []applyEntry{{Path: "new.txt", Action: "create", Content: "created"}}}
	first, err := d.Execute(context.Background(), verified(t, OpChangeSetApply, "approval-1", create, 7), nil)
	if err != nil {
		t.Fatalf("apply: %v", err)
	}

	// The manifest goes back to the backend as JSON and returns as JSON, so the round trip is
	// through bytes rather than through a Go value — which is what production does, and what
	// would break if `BackupManifest` ever grew a field that does not marshal.
	revert := revertArgs{Manifest: first.BackupManifest}
	if _, err := d.Execute(context.Background(), verified(t, OpChangeSetRevert, "approval-2", revert, 8), nil); err != nil {
		t.Fatalf("revert: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "new.txt")); err == nil {
		t.Error("the revert left the created file in place")
	}
	if content, err := os.ReadFile(existing); err != nil || string(content) != "original" {
		t.Errorf("the untouched file changed: %q, %v", content, err)
	}
}

func TestExecute_ARevertWithNoManifestIsRefused(t *testing.T) {
	d := newDispatcher(t, t.TempDir())
	_, err := d.Execute(context.Background(), verified(t, OpChangeSetRevert, "approval-1", revertArgs{}, 9), nil)
	if !isErr(err, ErrBadArgs) {
		t.Fatalf("err = %v, want ErrBadArgs", err)
	}
}

// ── The report, and the seam the session consumes ──────────────────────────────────────────

func TestOperations_IsDerivedFromTheTable(t *testing.T) {
	d := newDispatcher(t, t.TempDir())
	infos := d.Operations()
	if len(infos) != len(handlerTable) {
		t.Fatalf("Operations() reported %d of %d rows", len(infos), len(handlerTable))
	}
	implemented := 0
	for _, info := range infos {
		row := handlerTable[info.Operation]
		if info.Mutating != row.mutating || info.RequiresApproval != row.requiresApproval || info.Timeout != row.timeout {
			t.Errorf("%q: the report disagrees with the table", info.Operation)
		}
		if info.Implemented {
			implemented++
		}
	}
	// Pinned, so growth is deliberate and visible in a diff rather than drifting.
	//
	// Ten rows have bodies in this build: the two change-set operations, the six `validate.*`
	// validators built against real tools, `readiness.inventory` and `secretscan.run`. The scan pair
	// is deliberately absent from this count — their `implemented` is computed from whether an
	// indexer is wired, not from the table, and this dispatcher is constructed without one.
	//
	// Named individually rather than counted alone, because a count that matches for the wrong reason
	// is the failure this pin exists to catch.
	const expectedImplemented = 10
	if implemented != expectedImplemented {
		t.Errorf("%d operations report Implemented, expected %d: changeset.apply, changeset.revert, "+
			"the six validate.* operations, readiness.inventory and secretscan.run. "+
			"Update this number in the same commit as the new handler.", implemented, expectedImplemented)
	}
	for _, op := range []Operation{
		OpChangeSetApply, OpChangeSetRevert,
		OpValidateCompose, OpValidateK8s, OpValidateTofu, OpValidateHelm, OpValidateYAML, OpValidateTrivy,
		OpReadinessInventory, OpSecretScanRun,
	} {
		if !handlerTable[op].implemented {
			t.Errorf("%q is expected to be implemented and the table says otherwise", op)
		}
	}
}

func TestTheDispatcherFeedsSessionsRunnerThroughAFiveLineAdapter(t *testing.T) {
	// D-82 said the app wiring would need an adapter because `session.CommandRunner` owns its own
	// Progress and Outcome types. This is that adapter, written once and proved to compile and
	// run, so the cost is a known five lines rather than an unknown at wiring time.
	d := newDispatcher(t, t.TempDir())
	var runner session.CommandRunner = adapter{d}

	seen := []session.Progress{}
	outcome, err := runner.Execute(context.Background(),
		verified(t, OpScanFull, "", map[string]any{}, 10),
		func(p session.Progress) { seen = append(seen, p) })
	if err == nil {
		t.Fatal("scan.full is unimplemented; the adapter must pass its error through")
	}
	if outcome.Status != "" {
		t.Errorf("outcome = %+v on a failure path", outcome)
	}

	// And the success path, so the adapter's field copying and its progress bridge are both
	// exercised rather than only its error return.
	root := t.TempDir()
	live := adapter{newDispatcher(t, root)}
	args := applyArgs{Entries: []applyEntry{{Path: "b.txt", Action: "create", Content: "bridged"}}}
	outcome, err = live.Execute(context.Background(),
		verified(t, OpChangeSetApply, "approval-1", args, 11),
		func(p session.Progress) { seen = append(seen, p) })
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if outcome.Status != "succeeded" || outcome.Hashes["b.txt"] == "" || len(outcome.BackupManifest) == 0 {
		t.Errorf("the adapter dropped a field: %+v", outcome)
	}
	if len(seen) == 0 {
		t.Error("no progress crossed the adapter")
	}
}

// adapter is the five lines D-82 predicted.
type adapter struct{ d Dispatcher }

func (a adapter) Execute(
	ctx context.Context,
	v *envelope.Verified,
	progress func(session.Progress),
) (session.CommandOutcome, error) {
	result, err := a.d.Execute(ctx, v, SinkFunc(func(percent int, stage, message string) {
		progress(session.Progress{Percent: percent, Stage: stage, Message: message})
	}))
	if err != nil {
		return session.CommandOutcome{}, err
	}
	return session.CommandOutcome{
		Status:         result.Status,
		Output:         result.Output,
		BackupManifest: result.BackupManifest,
		Hashes:         result.Hashes,
	}, nil
}

func TestCode_MapsEveryExportedErrorToASuffix(t *testing.T) {
	for _, tc := range []struct {
		err  error
		want string
	}{
		{err: nil, want: ""},
		{err: ErrUnknownOperation, want: "operation-unknown"},
		{err: ErrUnimplemented, want: "operation-unimplemented"},
		{err: ErrApprovalRequired, want: "approval-required"},
		{err: ErrNoAuthority, want: "envelope-signature-invalid"},
		{err: ErrBadArgs, want: "envelope-malformed"},
		{err: fmt.Errorf("something else"), want: "operation-failed"},
	} {
		if got := Code(tc.err); got != tc.want {
			t.Errorf("Code(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}

func isErr(err, target error) bool {
	for err != nil {
		if err == target {
			return true
		}
		unwrapped, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = unwrapped.Unwrap()
	}
	return false
}
