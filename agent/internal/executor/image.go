// SPDX-License-Identifier: Apache-2.0

package executor

// Image build and push — Phase 2 §2.2.
//
// These are VERBS ON THE EXISTING `docker.image_action` AUTHORITY, not new operations. The authority is
// "act on a named image reference"; `pull`, `remove`, `build` and `push` are four things that authority
// does. Splitting per verb would mint four whitelist entries, four policy resources and four approval
// rows describing one permission, and the operation catalogue is the whitelist — it should name what a
// caller is allowed to affect, not how many ways there are to affect it.
//
// Two things here are genuinely different from `pull` and `remove`, and they are the whole reason this
// lives in its own file:
//
//  1. A BUILD READS THE FILESYSTEM. The build context and the Dockerfile are paths, and a path argument
//     is an escape unless it is contained. Both are resolved against the workspace root with the same
//     containment `clone` uses, so `../../..` and an absolute path outside the root are refused by the
//     same code that refuses them for a clone rather than by a second rule that could drift from it.
//
//  2. A PUSH CARRIES A CREDENTIAL. It arrives in the signed envelope, is handed to `docker login` on
//     STDIN — never in argv, where it would be readable in a process list by every other user on the
//     machine — and `docker logout` runs on every exit path. The credential is then asserted absent from
//     the docker config file, because `docker login` writes it there by default and a registry token
//     persisting on an operator's machine after one push is a credential leak with a long tail.
//
// What a push PRODUCES is the point of having it at all: a registry digest. A deployment that pins
// `myapp:latest` cannot be reproduced and one that pins `sha256:...` can. The digest is read back from
// the registry with `docker inspect` AFTER the push, never composed from the build output — a digest this
// code calculated is this code's opinion, and pinning an opinion as a fact is how a deployment record
// comes to describe an image that was never pushed.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/parag8487/ForgeOps/agent/internal/validator"
)

var (
	// ErrImageBuildContext refuses a build whose context or Dockerfile is outside the workspace root.
	ErrImageBuildContext = errors.New("executor: the build context is outside the workspace root")
	// ErrImageTagRequired refuses a build with no tag. An untagged build produces a dangling image that
	// nothing can reference and no deployment can pin, so it is a request with no outcome.
	ErrImageTagRequired = errors.New("executor: a build must name the tag it produces")
	// ErrRegistryCredentialRequired refuses a push to a registry that needs a credential without one.
	ErrRegistryCredentialRequired = errors.New("executor: this push names a registry user and no secret")
	// ErrRegistryCredentialPersisted is the assertion failing: the credential survived the push.
	ErrRegistryCredentialPersisted = errors.New("executor: the registry credential is still on disk")
	// ErrImageDigestAbsent refuses to report a successful push that produced no digest. A push whose
	// digest cannot be read has not been shown to have reached the registry, and reporting it as applied
	// would put an unverified image on a deployment record.
	ErrImageDigestAbsent = errors.New("executor: the push reported no digest, so it is not verified")
)

// imageBuildPushArgs extends the image action arguments for the two verbs that need more than a name.
//
// `RegistrySecret` is the field that must never be logged. It is deliberately NOT in the report type:
// the report is what travels back through the chokepoint into an audit row, and a struct that cannot
// carry the credential cannot leak it by someone adding a field to a log line later.
type imageBuildPushArgs struct {
	Action string `json:"action"`
	Image  string `json:"image"`
	// Build only. Relative to the workspace root, or absolute and beneath it. Empty means the root.
	BuildContext string `json:"build_context"`
	// Build only. Relative to the build context. Empty means `Dockerfile` inside it.
	Dockerfile string `json:"dockerfile"`
	// Build only. `--build-arg` pairs. Values are NOT redacted anywhere, so a caller putting a secret
	// here is putting it in the image's own metadata; the backend refuses secret-shaped names before
	// this is ever signed.
	BuildArgs map[string]string `json:"build_args"`
	// Push only. The registry hostname, for the login. Empty means Docker Hub's default.
	Registry string `json:"registry"`
	// Push only. The registry username.
	RegistryUser string `json:"registry_user"`
	// Push only. In the signed envelope, in memory, on stdin, never in argv.
	RegistrySecret string `json:"registry_secret"`
}

// ImageReport is what a build or a push reports. No credential field exists here by construction.
type ImageReport struct {
	Action string `json:"action"`
	Image  string `json:"image"`
	// Digest is the registry digest after a push, and the local image ID after a build. They are
	// DIFFERENT KINDS OF FACT and the field says which: a local ID is reproducible only on this
	// machine, and only the registry digest is worth pinning in a deployment record.
	Digest     string `json:"digest"`
	DigestKind string `json:"digest_kind"`
	// Registry is empty for a build. A build produces nothing outside this machine, and a report that
	// named a registry for it would suggest otherwise.
	Registry string `json:"registry"`
	// CredentialAbsent is the assertion's result, carried outward so the audit row records that the
	// check ran rather than leaving it as a claim in this comment.
	CredentialAbsent bool   `json:"credential_absent"`
	Output           string `json:"output"`
	ObservedAt       string `json:"observed_at"`
}

// resolveBuildContext contains the two path arguments to the workspace root.
//
// It reuses `resolveCloneTarget`'s containment by calling the same normalisation: the 8.3-short-name and
// `/private/var` mismatches that made clone's first containment check refuse valid roots apply here
// verbatim, and solving them twice would mean solving them differently.
func resolveBuildContext(root, context, dockerfile string) (absContext string, absDockerfile string, err error) {
	if strings.TrimSpace(context) == "" {
		absContext = root
	} else if filepath.IsAbs(context) {
		absContext = context
	} else {
		absContext = filepath.Join(root, context)
	}

	absRoot := normaliseDeepestExisting(root)
	absContext = normaliseDeepestExisting(absContext)
	relative, relErr := filepath.Rel(absRoot, absContext)
	if relErr != nil {
		return "", "", fmt.Errorf("%w: %q is not comparable to %q", ErrImageBuildContext, absContext, absRoot)
	}
	if relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", "", fmt.Errorf(
			"%w: %q is outside %q. The agent builds only from the workspace root it was started with",
			ErrImageBuildContext, absContext, absRoot)
	}

	name := strings.TrimSpace(dockerfile)
	if name == "" {
		name = "Dockerfile"
	}
	// The Dockerfile is contained against the BUILD CONTEXT, not the root. `docker build -f` accepts a
	// path outside the context, so without this a contained context with `-f ../../../etc/passwd` would
	// read a file the caller was never allowed to reach.
	if filepath.IsAbs(name) {
		absDockerfile = name
	} else {
		absDockerfile = filepath.Join(absContext, name)
	}
	absDockerfile = normaliseDeepestExisting(absDockerfile)
	relative, relErr = filepath.Rel(absContext, absDockerfile)
	if relErr != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", "", fmt.Errorf(
			"%w: the Dockerfile %q is outside the build context %q", ErrImageBuildContext, absDockerfile, absContext)
	}
	if _, statErr := os.Stat(absDockerfile); statErr != nil {
		return "", "", fmt.Errorf("%w: no Dockerfile at %q", ErrImageBuildContext, absDockerfile)
	}
	return absContext, absDockerfile, nil
}

// buildArgVector assembles `docker build`'s arguments deterministically.
//
// Sorted by name so the same request produces the same command line every time. That is not tidiness:
// the envelope is signed over the arguments and an operator comparing two audit rows for the same build
// needs them to differ only when the request differed.
func buildArgVector(image, absContext, absDockerfile string, buildArgs map[string]string) []string {
	vector := []string{"build", "-t", image, "-f", absDockerfile}
	names := make([]string, 0, len(buildArgs))
	for name := range buildArgs {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		vector = append(vector, "--build-arg", name+"="+buildArgs[name])
	}
	return append(vector, absContext)
}

// dockerImageBuild builds an image from a contained context.
func dockerImageBuild(ctx context.Context, d *dispatcher, args imageBuildPushArgs, sink ProgressSink) (Result, error) {
	if strings.TrimSpace(args.Image) == "" {
		return Result{}, ErrImageTagRequired
	}
	absContext, absDockerfile, err := resolveBuildContext(d.root, args.BuildContext, args.Dockerfile)
	if err != nil {
		return Result{}, err
	}
	runner, _, err := dockerRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	sink.Progress(20, string(OpDockerImageAction), "building "+args.Image)
	outcome, runErr := runner.Run(ctx, "docker", buildArgVector(args.Image, absContext, absDockerfile, args.BuildArgs)...)
	if runErr != nil || !outcome.Passed {
		// A BUILD FAILURE IS A RESULT, NOT AN ERROR of the operation — the same judgement `devtools.run`
		// makes about a failing test suite. The caller asked whether this Dockerfile builds; "no, and
		// here is the compiler output" answers the question. An error would discard the output, which is
		// the entire value of having asked.
		report := ImageReport{
			Action: "build", Image: args.Image, DigestKind: "none",
			Output: firstOf(tailOutput(outcome.Output)), ObservedAt: d.now().UTC().Format(time.RFC3339),
			CredentialAbsent: true,
		}
		encoded, marshalErr := json.Marshal(report)
		if marshalErr != nil {
			return Result{}, fmt.Errorf("executor: unencodable image report: %w", marshalErr)
		}
		sink.Progress(100, string(OpDockerImageAction), "build failed: "+args.Image)
		return Result{Status: "failed", Output: string(encoded)}, nil
	}

	report := ImageReport{
		Action: "build", Image: args.Image,
		Digest: localImageID(ctx, runner, args.Image), DigestKind: "local_image_id",
		Output: firstOf(tailOutput(outcome.Output)), ObservedAt: d.now().UTC().Format(time.RFC3339),
		CredentialAbsent: true,
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable image report: %w", err)
	}
	sink.Progress(100, string(OpDockerImageAction), "built "+args.Image)
	return Result{Status: "applied", Output: string(encoded)}, nil
}

// dockerImagePush pushes an image and reports the registry digest.
func dockerImagePush(ctx context.Context, d *dispatcher, args imageBuildPushArgs, sink ProgressSink) (Result, error) {
	if strings.TrimSpace(args.Image) == "" {
		return Result{}, ErrNoTarget
	}
	if strings.TrimSpace(args.RegistryUser) != "" && strings.TrimSpace(args.RegistrySecret) == "" {
		return Result{}, ErrRegistryCredentialRequired
	}
	runner, _, err := dockerRunner(ctx, d.root)
	if err != nil {
		return Result{}, err
	}

	authenticated := false
	if strings.TrimSpace(args.RegistryUser) != "" {
		sink.Progress(15, string(OpDockerImageAction), "authenticating to "+registryLabel(args.Registry))
		if loginErr := registryLogin(ctx, runner, args); loginErr != nil {
			return Result{}, loginErr
		}
		authenticated = true
		// Logout runs on EVERY exit path below, including the failures. A push that fails and leaves the
		// operator logged in is the leak this defers against, and it is the likelier case: the failure
		// paths are the ones nobody walks through by hand.
		defer func() { _, _ = runner.Run(ctx, "docker", "logout", registryHost(args.Registry)) }()
	}

	sink.Progress(45, string(OpDockerImageAction), "pushing "+args.Image)
	outcome, runErr := runner.Run(ctx, "docker", "push", args.Image)
	if runErr != nil || !outcome.Passed {
		return Result{}, fmt.Errorf("executor: docker push refused %s: %w — %s",
			args.Image, errOrRefused(runErr), firstLine(outcome.Output))
	}

	// THE DIGEST IS READ BACK, and its absence fails the operation. `docker push` prints a digest, and
	// parsing that line would be easier — but the printed line is the client's report of what it sent,
	// and `inspect`'s RepoDigests is what the local daemon recorded after the registry accepted it.
	digest := imageDigest(ctx, runner, args.Image)
	if digest == "" {
		return Result{}, fmt.Errorf("%w: %s", ErrImageDigestAbsent, args.Image)
	}

	credentialAbsent := true
	if authenticated {
		if assertErr := assertRegistryCredentialAbsent(args.RegistrySecret); assertErr != nil {
			// NOT downgraded to a warning on the report. A push that left the token in `config.json` has
			// done something the operator did not ask for and would not discover, so it fails loudly
			// here where the message can say which file to clear.
			return Result{}, assertErr
		}
	}

	report := ImageReport{
		Action: "push", Image: args.Image, Digest: digest, DigestKind: "registry_digest",
		Registry: registryLabel(args.Registry), CredentialAbsent: credentialAbsent,
		Output: firstOf(tailOutput(outcome.Output)), ObservedAt: d.now().UTC().Format(time.RFC3339),
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return Result{}, fmt.Errorf("executor: unencodable image report: %w", err)
	}
	sink.Progress(100, string(OpDockerImageAction), "pushed "+args.Image)
	return Result{Status: "applied", Output: string(encoded)}, nil
}

// registryLogin hands the secret to docker on STDIN.
//
// `--password-stdin` is the whole point. `docker login -p <secret>` puts the credential in argv, where
// `ps` shows it to every user on the machine and docker itself prints a deprecation warning saying so.
func registryLogin(ctx context.Context, runner *validator.Runner, args imageBuildPushArgs) error {
	vector := []string{"login", "--username", args.RegistryUser, "--password-stdin"}
	if host := registryHost(args.Registry); host != "" {
		vector = append(vector, host)
	}
	outcome, err := runner.RunWithStdin(ctx, args.RegistrySecret, "docker", vector...)
	if err != nil || !outcome.Passed {
		// The registry's own message is NOT included. It sometimes echoes the request, and this is the
		// one error path where the request contains the secret.
		return fmt.Errorf("executor: the registry refused the credential for %q at %s",
			args.RegistryUser, registryLabel(args.Registry))
	}
	return nil
}

// assertRegistryCredentialAbsent checks the docker config for the secret after a logout.
//
// This makes the "the credential does not persist" claim testable rather than asserted in a comment —
// the same shape as `assertNoCredentialOnDisk` for a clone, and for the same reason: the claim is about
// what is on the operator's disk, so only reading the disk establishes it.
func assertRegistryCredentialAbsent(secret string) error {
	if strings.TrimSpace(secret) == "" {
		return nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		// Unknown home means the check could not run, and an unrun check must not report success.
		return fmt.Errorf("%w: the docker config location is unknown, so its absence is unverified",
			ErrRegistryCredentialPersisted)
	}
	configPath := filepath.Join(home, ".docker", "config.json")
	contents, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			// No config file is the strongest possible pass.
			return nil
		}
		return fmt.Errorf("%w: %s is unreadable, so its absence is unverified",
			ErrRegistryCredentialPersisted, configPath)
	}
	if strings.Contains(string(contents), secret) {
		return fmt.Errorf("%w: clear %s", ErrRegistryCredentialPersisted, configPath)
	}
	// AND THE ENCODED FORM. docker stores `auths.<host>.auth` as base64 of `user:secret`, so searching
	// for base64 of the secret ALONE passes while the credential sits in the file encoded ? which is the
	// real-world case, and a check that reports clean on the real case is worse than no check. The first
	// version of this did exactly that and its own test caught it. Every auth value is therefore DECODED
	// and the plaintext searched, which needs no knowledge of the username and survives docker changing
	// how it composes the pair.
	var config struct {
		Auths map[string]struct {
			Auth     string `json:"auth"`
			Password string `json:"password"`
		} `json:"auths"`
	}
	if err := json.Unmarshal(contents, &config); err != nil {
		// An unparseable config cannot be cleared, and an unrun check must not report success.
		return fmt.Errorf("%w: %s is not decodable, so its absence is unverified",
			ErrRegistryCredentialPersisted, configPath)
	}
	for host, entry := range config.Auths {
		if entry.Password == secret {
			return fmt.Errorf("%w: clear the %s entry in %s", ErrRegistryCredentialPersisted, host, configPath)
		}
		decoded, decodeErr := base64.StdEncoding.DecodeString(entry.Auth)
		if decodeErr != nil {
			continue
		}
		if strings.Contains(string(decoded), secret) {
			return fmt.Errorf("%w: clear the %s entry in %s (stored base64-encoded)",
				ErrRegistryCredentialPersisted, host, configPath)
		}
	}
	return nil
}

// registryHost is what `docker login` and `docker logout` take. Empty means Docker Hub's default, which
// both commands accept as a missing argument rather than as an empty one.
func registryHost(registry string) string {
	return strings.TrimSpace(registry)
}

// registryLabel is what a report and a progress line say. Never empty, because "" in a panel reads as a
// missing value rather than as the default registry.
func registryLabel(registry string) string {
	if host := strings.TrimSpace(registry); host != "" {
		return host
	}
	return "docker.io"
}

// localImageID reads the image ID of a locally built image. Empty is tolerated here, unlike a push's
// digest: a build's ID is a convenience for the report and the build's success is already established by
// the exit status, whereas a push's digest is the evidence the registry accepted it.
func localImageID(ctx context.Context, runner *validator.Runner, reference string) string {
	outcome, err := runner.Run(ctx, "docker", "inspect", "--format", "{{.Id}}", reference)
	if err != nil || !outcome.Passed {
		return ""
	}
	return strings.TrimSpace(firstLine(outcome.Output))
}

// firstOf drops `tailOutput`'s truncation flag where the report does not carry one.
//
// A build log's TAIL is kept rather than its head, because a build fails at the end: the head is the base
// image pull, which is identical on every run and never the reason.
func firstOf(text string, _ bool) string { return text }
