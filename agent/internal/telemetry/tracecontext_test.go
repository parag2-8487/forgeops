// SPDX-License-Identifier: Apache-2.0
package telemetry

import (
	"context"
	"net/http"
	"testing"
)

func TestParseTraceparent_Valid(t *testing.T) {
	tc := ParseTraceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
	if tc == nil {
		t.Fatal("expected valid trace context")
	}
	if tc.TraceID != "4bf92f3577b34da6a3ce929d0e0e4736" {
		t.Errorf("TraceID = %q", tc.TraceID)
	}
	if tc.SpanID != "00f067aa0ba902b7" {
		t.Errorf("SpanID = %q", tc.SpanID)
	}
	if tc.TraceFlags != 0x01 {
		t.Errorf("TraceFlags = %02x", tc.TraceFlags)
	}
}

func TestParseTraceparent_Invalid(t *testing.T) {
	cases := []struct {
		name  string
		input string
	}{
		{"empty", ""},
		{"too short", "00-abc-def-01"},
		{"invalid version ff", "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
		{"all-zero trace-id", "00-00000000000000000000000000000000-00f067aa0ba902b7-01"},
		{"all-zero span-id", "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01"},
		{"uppercase hex", "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01"},
		{"wrong separators", "00:4bf92f3577b34da6a3ce929d0e0e4736:00f067aa0ba902b7:01"},
		{"garbage", "not-a-traceparent-at-all"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			result := ParseTraceparent(tc.input)
			if result != nil {
				t.Errorf("expected nil for %q, got %+v", tc.input, result)
			}
		})
	}
}

func TestChildContext_PreservesTraceID(t *testing.T) {
	parent := &TraceContext{
		TraceID:    "4bf92f3577b34da6a3ce929d0e0e4736",
		SpanID:     "00f067aa0ba902b7",
		TraceFlags: 0x01,
		TraceState: "vendorname=opaqueValue",
	}
	child := parent.ChildContext()

	if child.TraceID != parent.TraceID {
		t.Errorf("child TraceID = %q, want %q", child.TraceID, parent.TraceID)
	}
	if child.SpanID == parent.SpanID {
		t.Error("child SpanID must differ from parent")
	}
	if len(child.SpanID) != 16 {
		t.Errorf("child SpanID length = %d, want 16", len(child.SpanID))
	}
	if child.TraceState != parent.TraceState {
		t.Errorf("child TraceState = %q, want %q", child.TraceState, parent.TraceState)
	}
}

func TestFromHeaders_ValidTraceparent(t *testing.T) {
	h := http.Header{}
	h.Set("Traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
	h.Set("Tracestate", "congo=lZWRzIHRoNQKkCg")

	tc := FromHeaders(h)
	if tc.TraceID != "4bf92f3577b34da6a3ce929d0e0e4736" {
		t.Errorf("TraceID = %q", tc.TraceID)
	}
	// Span should differ (new child)
	if tc.SpanID == "00f067aa0ba902b7" {
		t.Error("SpanID should be a new child")
	}
	if tc.TraceState != "congo=lZWRzIHRoNQKkCg" {
		t.Errorf("TraceState = %q, want preserved", tc.TraceState)
	}
}

func TestFromHeaders_MalformedStartsFresh(t *testing.T) {
	h := http.Header{}
	h.Set("Traceparent", "this-is-garbage")

	tc := FromHeaders(h)
	if tc == nil {
		t.Fatal("expected non-nil context")
	}
	// Should be a fresh trace
	if len(tc.TraceID) != 32 {
		t.Errorf("TraceID length = %d, want 32", len(tc.TraceID))
	}
	if len(tc.SpanID) != 16 {
		t.Errorf("SpanID length = %d, want 16", len(tc.SpanID))
	}
}

func TestInjectHeaders(t *testing.T) {
	tc := &TraceContext{
		TraceID:    "4bf92f3577b34da6a3ce929d0e0e4736",
		SpanID:     "00f067aa0ba902b7",
		TraceFlags: 0x01,
		TraceState: "vendorname=value",
	}
	h := http.Header{}
	tc.InjectHeaders(h)

	expected := "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	if got := h.Get("Traceparent"); got != expected {
		t.Errorf("Traceparent = %q, want %q", got, expected)
	}
	if got := h.Get("Tracestate"); got != "vendorname=value" {
		t.Errorf("Tracestate = %q", got)
	}
}

func TestContextHelpers(t *testing.T) {
	tc := &TraceContext{
		TraceID: "abcdef1234567890abcdef1234567890",
		SpanID:  "1234567890abcdef",
	}
	ctx := WithContext(context.Background(), tc)
	got := FromContext(ctx)
	if got == nil {
		t.Fatal("expected trace context from context")
	}
	if got.TraceID != tc.TraceID {
		t.Errorf("TraceID = %q, want %q", got.TraceID, tc.TraceID)
	}
}

func TestFromContext_Nil(t *testing.T) {
	got := FromContext(context.Background())
	if got != nil {
		t.Error("expected nil from empty context")
	}
}

func TestNoopTracer(t *testing.T) {
	parent := &TraceContext{
		TraceID: "4bf92f3577b34da6a3ce929d0e0e4736",
		SpanID:  "00f067aa0ba902b7",
	}
	ctx := WithContext(context.Background(), parent)

	tracer := NoopTracer{}
	childCtx, span := tracer.StartSpan(ctx, "test-op")
	defer span.End()

	childTC := FromContext(childCtx)
	if childTC == nil {
		t.Fatal("expected child trace context")
	}
	if childTC.TraceID != parent.TraceID {
		t.Errorf("child TraceID = %q, want %q", childTC.TraceID, parent.TraceID)
	}
	if childTC.SpanID == parent.SpanID {
		t.Error("child SpanID must differ from parent")
	}
}
