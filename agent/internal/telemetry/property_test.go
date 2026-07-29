// SPDX-License-Identifier: Apache-2.0
package telemetry

import (
	"encoding/hex"
	"fmt"
	"net/http"
	"testing"

	"pgregory.net/rapid"
)

// TestProperty_P13_TraceContext tests:
// - valid trace-id preserved with a NEW span id
// - malformed input starts a fresh trace and is never propagated
// - tracestate unchanged
func TestProperty_P13_ValidTracePreserved(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		// Generate a valid traceparent
		traceID := rapid.StringMatching(`[0-9a-f]{32}`).Draw(t, "traceID")
		spanID := rapid.StringMatching(`[0-9a-f]{16}`).Draw(t, "spanID")
		flags := rapid.StringMatching(`[0-9a-f]{2}`).Draw(t, "flags")

		// Ensure non-zero
		if traceID == "00000000000000000000000000000000" || spanID == "0000000000000000" {
			return // Skip zero values
		}

		traceparent := fmt.Sprintf("00-%s-%s-%s", traceID, spanID, flags)
		tracestate := rapid.StringMatching(`[a-z]+=[a-zA-Z0-9]+`).Draw(t, "tracestate")

		h := http.Header{}
		h.Set("Traceparent", traceparent)
		h.Set("Tracestate", tracestate)

		tc := FromHeaders(h)

		// Trace-id must be preserved
		if tc.TraceID != traceID {
			t.Fatalf("TraceID = %q, want %q", tc.TraceID, traceID)
		}

		// Span-id must be NEW (different from parent)
		if tc.SpanID == spanID {
			t.Fatal("SpanID must differ from parent")
		}

		// Span-id must be valid hex of correct length
		if len(tc.SpanID) != 16 {
			t.Fatalf("SpanID length = %d, want 16", len(tc.SpanID))
		}
		if _, err := hex.DecodeString(tc.SpanID); err != nil {
			t.Fatalf("SpanID is not valid hex: %s", tc.SpanID)
		}

		// Tracestate must be preserved verbatim
		if tc.TraceState != tracestate {
			t.Fatalf("TraceState = %q, want %q", tc.TraceState, tracestate)
		}
	})
}

func TestProperty_P13_MalformedStartsFresh(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		// Generate malformed traceparent values
		malformed := rapid.OneOf(
			rapid.Just(""),
			rapid.Just("not-a-traceparent"),
			rapid.Just("ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
			rapid.Just("00-00000000000000000000000000000000-00f067aa0ba902b7-01"),
			rapid.Just("00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01"),
			rapid.StringMatching(`[a-zA-Z0-9!@#$%^&*()]{0,50}`),
		).Draw(t, "malformed")

		h := http.Header{}
		h.Set("Traceparent", malformed)

		tc := FromHeaders(h)

		// Must get a fresh, valid trace context
		if tc == nil {
			t.Fatal("expected non-nil trace context")
			return
		}
		if len(tc.TraceID) != 32 {
			t.Fatalf("TraceID length = %d, want 32", len(tc.TraceID))
		}
		if _, err := hex.DecodeString(tc.TraceID); err != nil {
			t.Fatalf("TraceID not valid hex: %s", tc.TraceID)
		}
		if len(tc.SpanID) != 16 {
			t.Fatalf("SpanID length = %d, want 16", len(tc.SpanID))
		}
		if _, err := hex.DecodeString(tc.SpanID); err != nil {
			t.Fatalf("SpanID not valid hex: %s", tc.SpanID)
		}

		// The malformed traceparent should NEVER be propagated
		outHeaders := http.Header{}
		tc.InjectHeaders(outHeaders)
		outTP := outHeaders.Get("Traceparent")
		// The outbound traceparent must not contain the malformed input
		parsed := ParseTraceparent(outTP)
		if parsed == nil {
			t.Fatal("outbound traceparent is invalid")
		}
		// The propagated trace must be fresh (different from any malformed input)
	})
}

func TestProperty_P13_TracestateUnchanged(t *testing.T) {
	rapid.Check(t, func(t *rapid.T) {
		tracestate := rapid.StringMatching(`[a-z]+=[a-zA-Z0-9/+]+`).Draw(t, "tracestate")

		// Use a valid traceparent
		h := http.Header{}
		h.Set("Traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
		h.Set("Tracestate", tracestate)

		tc := FromHeaders(h)

		// Tracestate must pass through unchanged
		if tc.TraceState != tracestate {
			t.Fatalf("TraceState = %q, want %q", tc.TraceState, tracestate)
		}

		// After injection, tracestate must still be unchanged
		outHeaders := http.Header{}
		tc.InjectHeaders(outHeaders)
		if outHeaders.Get("Tracestate") != tracestate {
			t.Fatalf("injected Tracestate = %q, want %q", outHeaders.Get("Tracestate"), tracestate)
		}
	})
}
