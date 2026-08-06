// SPDX-License-Identifier: Apache-2.0

package logging

import (
	"bytes"
	"strings"
	"testing"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// A configured secret must never reach captured output (design §7.2, §14.5, Q-24).
//
// The existing tests in logging_test.go cover bearer tokens and a plain configured
// value. These add the cases that matter once the agent starts handling other people's
// content in Phase 1: a secret appearing in a structured FIELD rather than the message,
// inside a wrapped ERROR, and embedded in a longer string such as a URL — which is how
// a git remote leaks a token.
//
// Credentials are synthetic, self-labelling and composed at runtime, per
// .antigravity/steering/secret-safety.md, so no contiguous literal here resembles a real token.

const syntheticMarker = "test-only-not-a-real-secret"

func syntheticSecret(suffix string) string { return syntheticMarker + "-" + suffix }

// captureRedacted builds a logger whose output lands in a buffer, so the encoded bytes
// can be inspected. It mirrors NewRedacted's core assembly; the sink is the only
// difference, which is what makes the assertion about redaction rather than about zap.
func captureRedacted(t *testing.T, secrets []string) (*zap.Logger, *bytes.Buffer) {
	t.Helper()

	var buf bytes.Buffer
	encCfg := zap.NewProductionEncoderConfig()
	encCfg.TimeKey = "ts"
	encCfg.EncodeTime = zapcore.ISO8601TimeEncoder

	core := zapcore.NewCore(
		zapcore.NewJSONEncoder(encCfg),
		zapcore.AddSync(&buf),
		zapcore.DebugLevel,
	)
	return zap.New(&redactCore{Core: core, secrets: secrets}), &buf
}

func TestRedactCore_SecretInAStructuredField(t *testing.T) {
	t.Parallel()

	secret := syntheticSecret("field")
	logger, buf := captureRedacted(t, []string{secret})

	logger.Info("cloning repository", zap.String("remote", "https://x-access-token:"+secret+"@github.com/o/r.git"))
	_ = logger.Sync()

	if strings.Contains(buf.String(), secret) {
		t.Fatalf("the secret survived in a structured field:\n%s", buf.String())
	}
	if !strings.Contains(buf.String(), "[REDACTED]") {
		t.Errorf("no redaction marker present:\n%s", buf.String())
	}
}

func TestRedactCore_SecretInsideAWrappedError(t *testing.T) {
	t.Parallel()

	// D-27's lesson on the Python side: the likeliest leak is an error message, because
	// transport libraries put the URL — with its credential — into the error text.
	secret := syntheticSecret("error")
	logger, buf := captureRedacted(t, []string{secret})

	logger.Error("push failed", zap.Error(errWithSecret(secret)))
	_ = logger.Sync()

	if strings.Contains(buf.String(), secret) {
		t.Fatalf("the secret survived inside an error field:\n%s", buf.String())
	}
}

func TestRedactCore_SecretEmbeddedInALongerString(t *testing.T) {
	t.Parallel()

	// Substring redaction, not whole-value equality: a token inside a URL, a JSON blob
	// or a command line must still be scrubbed.
	secret := syntheticSecret("embedded")
	logger, buf := captureRedacted(t, []string{secret})

	logger.Warn("validator output: Authorization: Bearer " + secret + " (truncated)")
	_ = logger.Sync()

	if strings.Contains(buf.String(), secret) {
		t.Fatalf("an embedded secret survived:\n%s", buf.String())
	}
}

func TestRedactCore_MultipleSecretsAreAllScrubbed(t *testing.T) {
	t.Parallel()

	first, second := syntheticSecret("one"), syntheticSecret("two")
	logger, buf := captureRedacted(t, []string{first, second})

	logger.Info("two credentials", zap.String("a", first), zap.String("b", second))
	_ = logger.Sync()

	for _, secret := range []string{first, second} {
		if strings.Contains(buf.String(), secret) {
			t.Errorf("%q survived:\n%s", secret, buf.String())
		}
	}
}

func TestRedactCore_UnrelatedTextIsUntouched(t *testing.T) {
	t.Parallel()

	// Redaction must not be indiscriminate. A core that scrubbed everything would pass
	// every test above while making the logs useless — the shape of the decorative
	// clause the Phase 0 review found in P-09.
	logger, buf := captureRedacted(t, []string{syntheticSecret("unused")})

	logger.Info("indexed 42 files", zap.String("project", "acme-api"))
	_ = logger.Sync()

	out := buf.String()
	for _, expected := range []string{"indexed 42 files", "acme-api"} {
		if !strings.Contains(out, expected) {
			t.Errorf("%q was removed from an unrelated log line:\n%s", expected, out)
		}
	}
	if strings.Contains(out, "[REDACTED]") {
		t.Errorf("unrelated text was redacted:\n%s", out)
	}
}

// errWithSecret returns an error whose message embeds the secret, the way a transport
// library's error wraps a credential-bearing URL.
func errWithSecret(secret string) error {
	return &leakyError{msg: "dial https://user:" + secret + "@example.invalid: refused"}
}

type leakyError struct{ msg string }

func (e *leakyError) Error() string { return e.msg }
