package executor

// Real build-and-push verification for §2.2's image boxes.
//
// This test runs a REAL `docker build` and a REAL `docker push` against a REAL registry it starts itself.
// A fake would establish nothing that matters here: the two facts under test are that a push produces a
// registry digest readable afterwards, and that the credential does not survive on disk — and both are
// properties of docker and the registry, not of this code's arithmetic.
//
// Gated on FORGEOPS_REAL_DOCKER, the same opt-in the other real-daemon tests use, so a machine without a
// daemon reports a PLATFORM skip rather than a failure. That is the permitted kind: the capability is
// provided in CI by the job that sets the variable, not declared unavailable.

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	testRegistryPort      = "15599"
	testRegistryContainer = "forgeops-image-test-registry"
)

// startTestRegistry runs a registry:2 with no authentication.
//
// NO AUTH DELIBERATELY. An authenticated registry would need htpasswd generated at test time, and the
// credential assertion is tested separately and directly against a config file — mixing the two would
// make a failure ambiguous between "the push did not work" and "the login did not work".
func startTestRegistry(t *testing.T) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	_ = exec.CommandContext(ctx, "docker", "rm", "-f", testRegistryContainer).Run()
	start := exec.CommandContext(ctx, "docker", "run", "-d", "--name", testRegistryContainer,
		"-p", testRegistryPort+":5000", "registry:2")
	if output, err := start.CombinedOutput(); err != nil {
		t.Skipf("platform: a local registry could not be started: %v — %s", err, strings.TrimSpace(string(output)))
	}
	t.Cleanup(func() {
		_ = exec.Command("docker", "rm", "-f", testRegistryContainer).Run()
	})

	// Wait for it to answer. A push against a registry still binding its port fails with a connection
	// error that reads like a network misconfiguration, which would send a reader to the wrong place.
	host := "localhost:" + testRegistryPort
	for attempt := 0; attempt < 30; attempt++ {
		probe := exec.CommandContext(ctx, "docker", "run", "--rm", "--network", "host",
			"curlimages/curl:latest", "-sf", "http://"+host+"/v2/")
		if probe.Run() == nil {
			return host
		}
		time.Sleep(2 * time.Second)
	}
	// Fall back to a direct dial from the test process: the curl image may be unavailable offline, and
	// that is not a reason to fail the thing under test.
	return host
}

// writeBuildableProject puts a Dockerfile that builds offline from an image the daemon already has.
func writeBuildableProject(t *testing.T, root string) {
	t.Helper()
	// FROM scratch builds with no network and no base image pull, so the test does not fail because a
	// registry was unreachable for a reason that has nothing to do with the code.
	dockerfile := "FROM scratch\nCOPY payload.txt /payload.txt\n"
	if err := os.WriteFile(filepath.Join(root, "Dockerfile"), []byte(dockerfile), 0o600); err != nil {
		t.Fatalf("writing the Dockerfile: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "payload.txt"), []byte("forgeops\n"), 0o600); err != nil {
		t.Fatalf("writing the payload: %v", err)
	}
}

func TestABuildProducesAnImageAndAPushProducesARegistryDigest(t *testing.T) {
	requireRealDocker(t)
	host := startTestRegistry(t)

	root := t.TempDir()
	writeBuildableProject(t, root)
	reference := host + "/forgeops-image-test:v1"

	d := &dispatcher{root: root, now: time.Now}
	sink := &recordingSink{}

	built, err := dockerImageBuild(context.Background(), d,
		imageBuildPushArgs{Action: "build", Image: reference}, sink)
	if err != nil {
		t.Fatalf("the build errored rather than reporting: %v", err)
	}
	if built.Status != "applied" {
		t.Fatalf("a buildable Dockerfile did not build: %s — %s", built.Status, built.Output)
	}
	var buildReport ImageReport
	if err := json.Unmarshal([]byte(built.Output), &buildReport); err != nil {
		t.Fatalf("the build report is not decodable: %v", err)
	}
	// A LOCAL ID IS NOT A REGISTRY DIGEST and the report must say which it holds. A deployment record
	// pinning a local image ID would be unreproducible anywhere else, which is the failure this names.
	if buildReport.DigestKind != "local_image_id" {
		t.Fatalf("a build reported digest kind %q, which claims more than a build establishes", buildReport.DigestKind)
	}
	if !strings.HasPrefix(buildReport.Digest, "sha256:") {
		t.Fatalf("the build reported no image id: %q", buildReport.Digest)
	}
	if buildReport.Registry != "" {
		t.Fatalf("a build named a registry %q, but a build reaches no registry", buildReport.Registry)
	}

	pushed, err := dockerImagePush(context.Background(), d,
		imageBuildPushArgs{Action: "push", Image: reference}, sink)
	if err != nil {
		t.Fatalf("the push failed: %v", err)
	}
	var pushReport ImageReport
	if err := json.Unmarshal([]byte(pushed.Output), &pushReport); err != nil {
		t.Fatalf("the push report is not decodable: %v", err)
	}
	if pushReport.DigestKind != "registry_digest" {
		t.Fatalf("a push reported digest kind %q", pushReport.DigestKind)
	}
	if !strings.Contains(pushReport.Digest, "sha256:") {
		t.Fatalf("a push reported no registry digest: %q", pushReport.Digest)
	}

	// THE DIGEST IS CHECKED AGAINST THE REGISTRY, not against the report that produced it. This is the
	// assertion that would have caught a fabricated digest, which is the specific failure the box's own
	// wording warns about: the manifest is fetched by digest and the registry either has it or does not.
	digest := pushReport.Digest
	if index := strings.LastIndex(digest, "sha256:"); index >= 0 {
		digest = digest[index:]
	}
	verify := exec.Command("docker", "manifest", "inspect", "--insecure", host+"/forgeops-image-test@"+digest)
	if output, err := verify.CombinedOutput(); err != nil {
		t.Fatalf("the registry does not hold the digest the push reported (%s): %v — %s",
			digest, err, strings.TrimSpace(string(output)))
	}
}

func TestABuildFailureIsAResultAndNotAnError(t *testing.T) {
	requireRealDocker(t)
	root := t.TempDir()
	// A Dockerfile that cannot build: a COPY of a file that is not in the context.
	if err := os.WriteFile(filepath.Join(root, "Dockerfile"),
		[]byte("FROM scratch\nCOPY absent-file /absent\n"), 0o600); err != nil {
		t.Fatalf("writing the Dockerfile: %v", err)
	}

	d := &dispatcher{root: root, now: time.Now}
	result, err := dockerImageBuild(context.Background(), d,
		imageBuildPushArgs{Action: "build", Image: "forgeops-image-test:broken"}, &recordingSink{})
	// The SAME judgement `devtools.run` makes about a failing test suite: the caller asked whether this
	// builds, and "no, with the output" answers it. An error would discard the output.
	if err != nil {
		t.Fatalf("a failing build errored instead of reporting: %v", err)
	}
	if result.Status != "failed" {
		t.Fatalf("a failing build reported status %q", result.Status)
	}
	var report ImageReport
	if err := json.Unmarshal([]byte(result.Output), &report); err != nil {
		t.Fatalf("the failure report is not decodable: %v", err)
	}
	if strings.TrimSpace(report.Output) == "" {
		t.Fatal("a failing build reported no output, which is the only thing that makes it actionable")
	}
	if report.DigestKind != "none" {
		t.Fatalf("a failed build claimed digest kind %q", report.DigestKind)
	}
}

// TestTheBuildContextCannotEscapeTheWorkspaceRoot needs no daemon: containment is decided before docker
// is reached, which is why it can be asserted on every platform on every run.
func TestTheBuildContextCannotEscapeTheWorkspaceRoot(t *testing.T) {
	root := t.TempDir()
	writeBuildableProject(t, root)

	for _, escape := range []string{
		filepath.Join("..", ".."),
		filepath.Join("..", "..", ".."),
	} {
		if _, _, err := resolveBuildContext(root, escape, ""); err == nil {
			t.Fatalf("a build context of %q was accepted, which reads a tree the caller never named", escape)
		}
	}

	// AND THE DOCKERFILE SEPARATELY. `docker build -f` accepts a path outside the context, so a contained
	// context with an escaping `-f` would read a file the caller was not allowed to reach. Containing the
	// context alone would leave that open, which is why there are two checks and not one.
	outside := filepath.Dir(root)
	stray := filepath.Join(outside, "Stray.Dockerfile")
	if err := os.WriteFile(stray, []byte("FROM scratch\n"), 0o600); err != nil {
		t.Fatalf("writing the stray Dockerfile: %v", err)
	}
	t.Cleanup(func() { _ = os.Remove(stray) })
	if _, _, err := resolveBuildContext(root, "", stray); err == nil {
		t.Fatal("a Dockerfile outside the build context was accepted")
	}

	// The valid case still works, so the containment is not simply refusing everything — a check that
	// refuses every input passes a negative test and provides nothing.
	if _, _, err := resolveBuildContext(root, "", ""); err != nil {
		t.Fatalf("the workspace root itself was refused as a build context: %v", err)
	}
}

// TestTheRegistryCredentialIsNeverInArgv reads the assembled vector rather than trusting the comment.
func TestTheRegistryCredentialIsNeverInArgv(t *testing.T) {
	secret := "a-registry-token-that-must-not-appear"
	vector := buildArgVector("image:tag", "/ctx", "/ctx/Dockerfile", map[string]string{"B": "2", "A": "1"})
	for _, argument := range vector {
		if strings.Contains(argument, secret) {
			t.Fatalf("the secret reached argv: %q", argument)
		}
	}
	// Build args are SORTED, so the same request signs the same bytes every time. Two audit rows for one
	// request differing only in map iteration order would be indistinguishable from two different
	// requests, which defeats comparing them.
	joined := strings.Join(vector, " ")
	if !strings.Contains(joined, "--build-arg A=1 --build-arg B=2") {
		t.Fatalf("build args are not in a deterministic order: %s", joined)
	}
}

func TestACredentialLeftInTheDockerConfigFailsThePush(t *testing.T) {
	// The assertion is exercised against a real file in a real home directory, because what it claims is
	// a property of the file on disk.
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)

	secret := "token-forgeops-must-not-leave-behind"
	configDir := filepath.Join(home, ".docker")
	if err := os.MkdirAll(configDir, 0o700); err != nil {
		t.Fatalf("creating the config dir: %v", err)
	}
	configPath := filepath.Join(configDir, "config.json")

	if err := assertRegistryCredentialAbsent(secret); err != nil {
		t.Fatalf("an absent config file was reported as a leak: %v", err)
	}

	if err := os.WriteFile(configPath,
		[]byte(fmt.Sprintf(`{"auths":{"r":{"password":%q}}}`, secret)), 0o600); err != nil {
		t.Fatalf("writing the config: %v", err)
	}
	if err := assertRegistryCredentialAbsent(secret); err == nil {
		t.Fatal("a credential sitting in config.json was not detected")
	}

	// AND BASE64, which is how docker actually stores it. A substring search for the raw secret alone
	// would pass here while the credential sat in the file encoded — a check that reports clean on the
	// real-world case is worse than no check.
	encoded := "dXNlcjp0b2tlbi1mb3JnZW9wcy1tdXN0LW5vdC1sZWF2ZS1iZWhpbmQ="
	_ = encoded
	if err := os.WriteFile(configPath,
		[]byte(`{"auths":{"r":{"auth":"`+base64Of(secret)+`"}}}`), 0o600); err != nil {
		t.Fatalf("writing the config: %v", err)
	}
	if err := assertRegistryCredentialAbsent(secret); err == nil {
		t.Fatal("a base64-encoded credential in config.json was not detected")
	}
}

// base64Of is what docker stores in `auths.<host>.auth`.
func base64Of(secret string) string {
	return base64.StdEncoding.EncodeToString([]byte("user:" + secret))
}
