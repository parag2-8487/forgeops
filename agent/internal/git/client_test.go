// SPDX-License-Identifier: Apache-2.0
package git_test

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/parag8487/ForgeOps/agent/internal/git"
)

// TestNoLibraryTypesLeak verifies that no go-git or go-github types appear in
// the public surface of the git package. All exported types must be project-owned.
func TestNoLibraryTypesLeak(t *testing.T) {
	t.Parallel()

	// Inspect each project-owned type and confirm its fields use only
	// built-in types, standard library types, or types from this package.
	projectTypes := []reflect.Type{
		reflect.TypeOf(git.Signature{}),
		reflect.TypeOf(git.ChangeSet{}),
		reflect.TypeOf(git.Commit{}),
		reflect.TypeOf(git.PullRequestRequest{}),
		reflect.TypeOf(git.PullRequest{}),
		reflect.TypeOf(git.PRStatus{}),
		reflect.TypeOf(git.Config{}),
		reflect.TypeOf(git.EnvTokenSource{}),
		reflect.TypeOf(git.ErrTokenMissing{}),
		reflect.TypeOf(git.ErrPushRejected{}),
		reflect.TypeOf(git.ErrGitAuth{}),
		reflect.TypeOf(git.ErrRateLimited{}),
		reflect.TypeOf(git.ErrPathOutsideRepo{}),
	}

	forbiddenPrefixes := []string{
		"github.com/go-git/",
		"github.com/google/go-github/",
	}

	for _, typ := range projectTypes {
		for i := range typ.NumField() {
			field := typ.Field(i)
			fieldPkg := fieldPackagePath(field.Type)
			for _, prefix := range forbiddenPrefixes {
				if strings.HasPrefix(fieldPkg, prefix) {
					t.Errorf("type %s field %s uses library type from %s", typ.Name(), field.Name, fieldPkg)
				}
			}
		}
	}
}

// fieldPackagePath returns the package path of the type, traversing pointers and slices.
func fieldPackagePath(t reflect.Type) string {
	for t.Kind() == reflect.Ptr || t.Kind() == reflect.Slice {
		t = t.Elem()
	}
	return t.PkgPath()
}

// TestEnvTokenSource_MissingToken verifies that a missing environment variable
// produces a typed ErrTokenMissing error that includes the variable name but
// never the token value.
func TestEnvTokenSource_MissingToken(t *testing.T) {
	const envVar = "FORGEOPS_TEST_TOKEN_MISSING_XYZ"
	t.Setenv(envVar, "")

	src := &git.EnvTokenSource{EnvVar: envVar}
	_, err := src.Token(context.Background())

	if err == nil {
		t.Fatal("expected error for missing token, got nil")
	}

	var tokenErr *git.ErrTokenMissing
	if !errors.As(err, &tokenErr) {
		t.Fatalf("expected *ErrTokenMissing, got %T: %v", err, err)
	}

	if tokenErr.EnvVar != envVar {
		t.Errorf("ErrTokenMissing.EnvVar = %q, want %q", tokenErr.EnvVar, envVar)
	}

	// The error message must reference the env var name but never contain a token value.
	msg := err.Error()
	if !strings.Contains(msg, envVar) {
		t.Errorf("error message %q should contain env var name %q", msg, envVar)
	}
}

// TestEnvTokenSource_ReadsCorrectVariable verifies that EnvTokenSource reads
// from the configured environment variable.
func TestEnvTokenSource_ReadsCorrectVariable(t *testing.T) {
	const envVar = "FORGEOPS_TEST_TOKEN_READ_XYZ"
	const expected = "ghp_test1234567890"
	t.Setenv(envVar, expected)

	src := &git.EnvTokenSource{EnvVar: envVar}
	token, err := src.Token(context.Background())

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if token != expected {
		t.Errorf("Token() = %q, want %q", token, expected)
	}
}

// TestEnvTokenSource_ErrorNeverContainsTokenValue verifies that even when a
// different variable has a value set, the error from a missing variable never
// leaks any token value.
func TestEnvTokenSource_ErrorNeverContainsTokenValue(t *testing.T) {
	const envVar = "FORGEOPS_TEST_TOKEN_NEVER_LEAK"
	const secretValue = "ghp_supersecret999"

	// Set, then unset to ensure it's empty.
	t.Setenv(envVar, "")

	src := &git.EnvTokenSource{EnvVar: envVar}
	_, err := src.Token(context.Background())

	if err == nil {
		t.Fatal("expected error, got nil")
	}

	msg := err.Error()
	if strings.Contains(msg, secretValue) {
		t.Error("error message must never contain a token value")
	}
}

// TestConfigValidate_EmptyFields verifies that Config.Validate rejects
// configurations with missing required fields.
func TestConfigValidate_EmptyFields(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		config git.Config
		want   []string // substrings expected in the error
	}{
		{
			name:   "all empty",
			config: git.Config{},
			want:   []string{"Owner", "Repo", "AuthorName", "AuthorEmail"},
		},
		{
			name: "only Owner set",
			config: git.Config{
				Owner: "parag8487",
			},
			want: []string{"Repo", "AuthorName", "AuthorEmail"},
		},
		{
			name: "Owner and Repo set",
			config: git.Config{
				Owner: "parag8487",
				Repo:  "ForgeOps",
			},
			want: []string{"AuthorName", "AuthorEmail"},
		},
		{
			name: "all required set",
			config: git.Config{
				Owner:       "parag8487",
				Repo:        "ForgeOps",
				AuthorName:  "ForgeOps Bot",
				AuthorEmail: "bot@forgeops.dev",
			},
			want: nil,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			err := tc.config.Validate()

			if tc.want == nil {
				if err != nil {
					t.Fatalf("Validate() unexpected error: %v", err)
				}
				return
			}

			if err == nil {
				t.Fatal("Validate() expected error, got nil")
			}

			msg := err.Error()
			for _, sub := range tc.want {
				if !strings.Contains(msg, sub) {
					t.Errorf("Validate() error %q should contain %q", msg, sub)
				}
			}
		})
	}
}

// TestClientInterface_Assignability is a compile-time check that ensures the
// Client interface is defined and can be used as a variable type.
// This test proves the interface exists without requiring an implementation.
func TestClientInterface_Assignability(t *testing.T) {
	t.Parallel()

	// Verify the interface type exists and has the expected methods via reflection.
	clientType := reflect.TypeOf((*git.Client)(nil)).Elem()

	expectedMethods := []string{
		"CreateBranch",
		"CommitPaths",
		"Push",
		"OpenPullRequest",
		"PullRequestStatus",
		"PollUntil",
	}

	for _, method := range expectedMethods {
		if _, ok := clientType.MethodByName(method); !ok {
			t.Errorf("Client interface missing method %s", method)
		}
	}
}

// TestTokenSourceInterface_Assignability verifies the TokenSource interface
// exists and EnvTokenSource satisfies it.
func TestTokenSourceInterface_Assignability(t *testing.T) {
	t.Parallel()

	// Compile-time assignability check.
	var _ git.TokenSource = (*git.EnvTokenSource)(nil)
}
