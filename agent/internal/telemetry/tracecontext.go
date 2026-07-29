// SPDX-License-Identifier: Apache-2.0
package telemetry

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"regexp"
)

// TraceContext holds W3C trace context fields.
type TraceContext struct {
	TraceID    string
	SpanID     string
	TraceFlags byte
	TraceState string // preserved verbatim
}

// contextKey is unexported to avoid collisions.
type contextKey struct{}

var traceCtxKey = contextKey{}

// validTraceparent matches: version(2hex)-traceid(32hex)-spanid(16hex)-flags(2hex)
var validTraceparent = regexp.MustCompile(`^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$`)

// zeroTraceID is an invalid trace-id per spec
const zeroTraceID = "00000000000000000000000000000000"
const zeroSpanID = "0000000000000000"

// ParseTraceparent parses a W3C traceparent header value.
// Returns nil if the value is malformed (caller should start a fresh trace).
func ParseTraceparent(value string) *TraceContext {
	matches := validTraceparent.FindStringSubmatch(value)
	if matches == nil {
		return nil
	}

	version := matches[1]
	traceID := matches[2]
	spanID := matches[3]
	flags := matches[4]

	// Version 255 (ff) is invalid
	if version == "ff" {
		return nil
	}
	// All-zero trace-id or span-id is invalid
	if traceID == zeroTraceID || spanID == zeroSpanID {
		return nil
	}

	flagByte, _ := hex.DecodeString(flags)
	return &TraceContext{
		TraceID:    traceID,
		SpanID:     spanID,
		TraceFlags: flagByte[0],
	}
}

// NewSpanID generates a random 16-hex-character span id.
func NewSpanID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// NewTraceID generates a random 32-hex-character trace id.
func NewTraceID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// ChildContext creates a child span from a parent trace context.
// The trace-id is preserved, a new span-id is minted.
func (tc *TraceContext) ChildContext() *TraceContext {
	return &TraceContext{
		TraceID:    tc.TraceID,
		SpanID:     NewSpanID(),
		TraceFlags: tc.TraceFlags,
		TraceState: tc.TraceState,
	}
}

// Traceparent returns the formatted W3C traceparent header value.
func (tc *TraceContext) Traceparent() string {
	return fmt.Sprintf("00-%s-%s-%02x", tc.TraceID, tc.SpanID, tc.TraceFlags)
}

// Fresh creates a completely new trace context (new trace-id and span-id).
func Fresh() *TraceContext {
	return &TraceContext{
		TraceID:    NewTraceID(),
		SpanID:     NewSpanID(),
		TraceFlags: 0x01, // sampled
	}
}

// FromHeaders extracts trace context from HTTP headers.
// Returns a valid context (fresh if inbound is malformed), with tracestate preserved.
func FromHeaders(h http.Header) *TraceContext {
	tp := h.Get("Traceparent")
	tc := ParseTraceparent(tp)
	if tc == nil {
		// Malformed: start fresh, never propagate the malformed value
		tc = Fresh()
	} else {
		// Mint a new span id for this hop
		tc = tc.ChildContext()
	}
	tc.TraceState = h.Get("Tracestate")
	return tc
}

// InjectHeaders sets traceparent and tracestate on outbound headers.
func (tc *TraceContext) InjectHeaders(h http.Header) {
	h.Set("Traceparent", tc.Traceparent())
	if tc.TraceState != "" {
		h.Set("Tracestate", tc.TraceState)
	}
}

// WithContext stores a TraceContext in a context.Context.
func WithContext(ctx context.Context, tc *TraceContext) context.Context {
	return context.WithValue(ctx, traceCtxKey, tc)
}

// FromContext retrieves the TraceContext from a context.Context.
// Returns nil if none is stored.
func FromContext(ctx context.Context) *TraceContext {
	tc, _ := ctx.Value(traceCtxKey).(*TraceContext)
	return tc
}

// Tracer is the telemetry interface. Phase 0 has only NoopTracer.
type Tracer interface {
	// StartSpan creates a child span from the context's trace.
	StartSpan(ctx context.Context, name string) (context.Context, SpanEnder)
}

// SpanEnder ends a span.
type SpanEnder interface {
	End()
}

// NoopTracer propagates context but records nothing.
type NoopTracer struct{}

func (NoopTracer) StartSpan(ctx context.Context, _ string) (context.Context, SpanEnder) {
	tc := FromContext(ctx)
	if tc == nil {
		tc = Fresh()
	} else {
		tc = tc.ChildContext()
	}
	return WithContext(ctx, tc), noopSpan{}
}

type noopSpan struct{}

func (noopSpan) End() {}
