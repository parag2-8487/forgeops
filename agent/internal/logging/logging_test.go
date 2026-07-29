// SPDX-License-Identifier: Apache-2.0
package logging

import (
	"bytes"
	"strings"
	"testing"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"go.uber.org/zap/zaptest/observer"
)

func TestNew_ConsoleFormat(t *testing.T) {
	logger, err := New("DEBUG", "console")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer logger.Sync()
	// Should not panic
	logger.Info("test message", zap.String("component", "test"))
}

func TestNew_JSONFormat(t *testing.T) {
	logger, err := New("INFO", "json")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer logger.Sync()
	logger.Info("test message", zap.String("component", "test"))
}

func TestNew_RequiredFields(t *testing.T) {
	// Use an observer to capture logged entries
	core, obs := observer.New(zapcore.DebugLevel)
	logger := zap.New(core, zap.AddCaller())

	logger.Info("hello",
		zap.String("component", "mycomp"),
	)

	entries := obs.All()
	if len(entries) != 1 {
		t.Fatalf("expected 1 entry, got %d", len(entries))
	}
	e := entries[0]
	// Check required fields present
	if e.Message != "hello" {
		t.Errorf("msg = %q, want hello", e.Message)
	}
	if e.Level != zapcore.InfoLevel {
		t.Errorf("level = %v, want info", e.Level)
	}
	if e.Caller.File == "" {
		t.Error("caller should be present")
	}
	// Check component field
	found := false
	for _, f := range e.Context {
		if f.Key == "component" && f.String == "mycomp" {
			found = true
		}
	}
	if !found {
		t.Error("component field not found in context")
	}
}

func TestNew_LoggerNaming(t *testing.T) {
	core, obs := observer.New(zapcore.DebugLevel)
	logger := zap.New(core).Named("fileops")
	logger.Info("test")

	entries := obs.All()
	if len(entries) != 1 {
		t.Fatalf("expected 1 entry, got %d", len(entries))
	}
	if entries[0].LoggerName != "fileops" {
		t.Errorf("logger name = %q, want fileops", entries[0].LoggerName)
	}
}

func TestRedactCore_BearerTokens(t *testing.T) {
	var buf bytes.Buffer
	enc := zapcore.NewJSONEncoder(zap.NewProductionEncoderConfig())
	ws := zapcore.AddSync(&buf)
	core := zapcore.NewCore(enc, ws, zapcore.DebugLevel)

	redacting := &redactCore{
		Core:    core,
		secrets: nil,
	}
	logger := zap.New(redacting)

	logger.Info("auth: Bearer sk-secret-token-12345 done")
	logger.Sync()

	output := buf.String()
	if strings.Contains(output, "sk-secret-token-12345") {
		t.Errorf("bearer token leaked in output: %s", output)
	}
	if !strings.Contains(output, "[REDACTED]") {
		t.Errorf("expected [REDACTED] in output: %s", output)
	}
}

func TestRedactCore_ConfiguredSecrets(t *testing.T) {
	var buf bytes.Buffer
	enc := zapcore.NewJSONEncoder(zap.NewProductionEncoderConfig())
	ws := zapcore.AddSync(&buf)
	core := zapcore.NewCore(enc, ws, zapcore.DebugLevel)

	redacting := &redactCore{
		Core:    core,
		secrets: []string{"my-super-secret-value", "another-secret"},
	}
	logger := zap.New(redacting)

	logger.Info("connecting with key my-super-secret-value to server")
	logger.Info("field test", zap.String("token", "another-secret"))
	logger.Sync()

	output := buf.String()
	if strings.Contains(output, "my-super-secret-value") {
		t.Errorf("secret leaked in message: %s", output)
	}
	if strings.Contains(output, "another-secret") {
		t.Errorf("secret leaked in field: %s", output)
	}
}

func TestRedactCore_NoFalseRedaction(t *testing.T) {
	var buf bytes.Buffer
	enc := zapcore.NewJSONEncoder(zap.NewProductionEncoderConfig())
	ws := zapcore.AddSync(&buf)
	core := zapcore.NewCore(enc, ws, zapcore.DebugLevel)

	redacting := &redactCore{
		Core:    core,
		secrets: []string{"secret123"},
	}
	logger := zap.New(redacting)

	logger.Info("this is a normal message with no secrets")
	logger.Sync()

	output := buf.String()
	if strings.Contains(output, "[REDACTED]") {
		t.Errorf("false redaction in output: %s", output)
	}
	if !strings.Contains(output, "normal message") {
		t.Errorf("message content missing: %s", output)
	}
}
