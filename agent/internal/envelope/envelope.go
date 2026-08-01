// SPDX-License-Identifier: Apache-2.0

// Package envelope owns the command envelope, its canonical form, and the
// capability type that proves one was verified (design §7.6, §10.4, §17.1 D-59).
//
// Why this is its own package
// ---------------------------
// D-59. `internal/session` holds the Manager, and the Manager holds a dispatcher, so
// `session` imports `executor` (§10.1, §10.3). The mutation boundary
// `executor/internal/mutate` must take the verified-envelope type as its argument, so if
// that type lived in `session` the import graph would close:
//
//	session -> executor -> executor/internal/mutate -> session
//
// Go rejects that at compile time. This package therefore imports NOTHING from
// `internal/**` — it is a leaf — which is what makes it safe for every layer above to
// depend on. A test asserts the leaf property directly, because it is the reason the
// package exists and a single future import would quietly undo it.
//
// What lives here and what does not
// ---------------------------------
// Here: the wire shape, RFC 8785 canonicalisation, the domain-separated signing input,
// HMAC verification, the six-step Verify order, and the typed errors. Not here:
// anything that knows how an envelope arrived (that is `session`), and anything that
// knows what an operation does (that is `executor`).
package envelope

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"unicode/utf16"
)

// Version, DomainPrefix, ApprovalDomainPrefix and SigningInput live in `domain.go`, extracted so
// Q-14's negative control can overlay the domain-separation seam without carrying a copy of this
// whole file. See the comment there.

// Operation is a member of the closed named-operation catalogue (§7.7).
//
// Deliberately a string type rather than an int enum: the value travels over the wire
// and appears in audit rows, so an added member must not renumber an existing one.
type Operation string

// PolicyContext binds an envelope to the exact policy bundle that authorised it.
//
// `BundleDigest` is what Q-07 compares against the agent's loaded bundle. It is a
// required member: an envelope that names no bundle cannot be checked against one, and
// "no digest" must not be readable as "any digest".
type PolicyContext struct {
	BundleDigest string `json:"bundle_digest"`
	Decision     string `json:"decision"`
}

// Envelope is the §7.6 wire shape, exactly and in full.
//
// Every member is present in the struct even where a given operation does not use it,
// because canonicalisation is defined over the member set: an omitted member and an
// empty one produce different bytes, so which is which cannot be left to a caller.
//
// `Args` is json.RawMessage rather than map[string]any on purpose. Unmarshalling into a
// map and re-marshalling would silently turn every JSON number into a float64, and §7.6
// states that no envelope contains a float — the exact corner RFC 8785 is hardest at,
// and the one where two runtimes are most likely to disagree. Keeping the raw bytes
// means the canonicaliser sees the numbers the sender actually wrote.
type Envelope struct {
	V             string          `json:"v"`
	CommandID     string          `json:"command_id"`
	DeviceID      string          `json:"device_id"`
	Operation     Operation       `json:"operation"`
	Args          json.RawMessage `json:"args"`
	ApprovalID    string          `json:"approval_id"`
	PolicyContext PolicyContext   `json:"policy_context"`
	Nonce         string          `json:"nonce"`
	Seq           int64           `json:"seq"`
	NotAfter      int64           `json:"not_after"`

	// Signature is base64url(HMAC-SHA256(key, signing input)). It is removed before
	// canonicalisation and therefore never covered by its own value.
	Signature string `json:"signature,omitempty"`
}

// MaxSafeInteger is the largest integer RFC 8785 serialises exactly.
//
// The scheme defines numbers through ES6 `Number`, an IEEE-754 double, so 2^53 is already
// unrepresentable. This bound exists here because the two runtimes disagreed without it, and
// the disagreement was worse than a byte difference: `rfc8785` on the Python side raises
// IntegerDomainError above it, while the serialiser below writes the decimal digits verbatim
// and produces bytes happily. One side would report "malformed document" and the other
// "signature invalid" for the same envelope, and an operator cannot tell those apart.
//
// Mirrors `backend/src/governance/envelope.py::MAX_SAFE_INTEGER`.
const MaxSafeInteger int64 = 1<<53 - 1

// Errors returned by this package. Each maps to an RFC 9457 suffix and an `agent.error`
// code through Code() below (Appendix C.1, C.2).
var (
	ErrSchema        = errors.New("envelope: schema invalid")
	ErrFloatValue    = errors.New("envelope: float values are not permitted")
	ErrIntegerDomain = errors.New("envelope: integer is outside RFC 8785's exact domain")
	ErrExpired       = errors.New("envelope: not_after has passed")
	ErrTooFarFuture  = errors.New("envelope: not_after exceeds the maximum age")
	ErrSignature     = errors.New("envelope: signature does not verify")
	ErrReplayNonce   = errors.New("envelope: nonce already seen")
	ErrReplaySeq     = errors.New("envelope: seq is not greater than the last accepted seq")
	ErrPolicyStale   = errors.New("envelope: policy_context digest does not match the loaded bundle")
	ErrNoReplayGuard = errors.New("envelope: a Verifier requires a ReplayGuard")
	ErrNoBundle      = errors.New("envelope: a Verifier requires a BundleDigestSource")
)

// canonicalMembers is the member set §7.6 enumerates, in the order the spec lists them.
//
// Held as data rather than derived by reflection over struct tags. Reflection would make
// the signed member set an accident of field order and json tags — a field renamed for
// tidiness would change every signature ever produced. This list is the contract, and
// canonicalBody below fails loudly if the struct and the list disagree.
var canonicalMembers = []string{
	"v", "command_id", "device_id", "operation", "args",
	"approval_id", "policy_context", "nonce", "seq", "not_after",
}

// CanonicalBytes returns the RFC 8785 (JCS) serialisation of e with `signature` absent.
//
// Exported for exactly one reason, which §10.4 states: the cross-runtime fixture corpus.
// `backend/src/governance/envelope.py` must produce byte-identical output for the same
// logical envelope, and the only way to keep that true over time is for both sides to be
// tested against the same committed vectors (Q-14).
func CanonicalBytes(e Envelope) ([]byte, error) {
	body, err := canonicalBody(e)
	if err != nil {
		return nil, err
	}
	return jcs(body)
}

// Digest returns the hex SHA-256 of the envelope's signing input.
//
// This is the value `MutationAuthority.envelope_digest` carries, so an audit row names
// the exact bytes that were signed rather than a re-serialisation of them.
func Digest(e Envelope) (string, error) {
	input, err := SigningInput(DomainPrefix, e)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(input)
	return hex.EncodeToString(sum[:]), nil
}

// canonicalBody converts an Envelope into the ordered map JCS serialises, with
// `signature` absent, and refuses any float anywhere in `args`.
func canonicalBody(e Envelope) (map[string]any, error) {
	if err := requireNoFloat(e.Args); err != nil {
		return nil, err
	}
	// `seq` and `not_after` are int64 on the wire, so they can hold values RFC 8785 cannot
	// serialise exactly. Checked here rather than in Verify, because canonicalisation is what
	// the bound protects: an envelope whose seq is 2^53 has no agreed canonical form at all.
	if err := requireSafeInteger("seq", e.Seq); err != nil {
		return nil, err
	}
	if err := requireSafeInteger("not_after", e.NotAfter); err != nil {
		return nil, err
	}

	var args any
	if len(e.Args) == 0 {
		// An absent `args` is normalised to an empty object rather than to null. §7.7's
		// operations all take an object, and leaving the choice to the sender would mean
		// two different canonical forms for one logical envelope.
		args = map[string]any{}
	} else {
		decoder := json.NewDecoder(strings.NewReader(string(e.Args)))
		decoder.UseNumber()
		if err := decoder.Decode(&args); err != nil {
			return nil, fmt.Errorf("%w: args is not valid JSON: %v", ErrSchema, err)
		}
		// The type is checked rather than assumed. `Args` is json.RawMessage, so an array, a
		// string or a number all unmarshal without complaint and canonicalise to perfectly
		// valid bytes that mean nothing — while the Python side refuses them. Two runtimes
		// that disagree about which documents exist is the failure this whole package is
		// arranged to prevent.
		if _, ok := args.(map[string]any); !ok {
			return nil, fmt.Errorf("%w: args must be a JSON object (§7.7's operations all take "+
				"one), got %T", ErrSchema, args)
		}
	}

	body := map[string]any{
		"v":           e.V,
		"command_id":  e.CommandID,
		"device_id":   e.DeviceID,
		"operation":   string(e.Operation),
		"args":        args,
		"approval_id": e.ApprovalID,
		"policy_context": map[string]any{
			"bundle_digest": e.PolicyContext.BundleDigest,
			"decision":      e.PolicyContext.Decision,
		},
		"nonce":     e.Nonce,
		"seq":       json.Number(fmt.Sprintf("%d", e.Seq)),
		"not_after": json.Number(fmt.Sprintf("%d", e.NotAfter)),
	}

	// The struct and canonicalMembers must describe the same member set. If they ever
	// diverge, every signature this package produces changes meaning, so the mismatch is
	// an error rather than a silent difference.
	if len(body) != len(canonicalMembers) {
		return nil, fmt.Errorf("%w: canonical body has %d members, the §7.6 list has %d",
			ErrSchema, len(body), len(canonicalMembers))
	}
	for _, member := range canonicalMembers {
		if _, ok := body[member]; !ok {
			return nil, fmt.Errorf("%w: canonical body is missing the §7.6 member %q", ErrSchema, member)
		}
	}
	return body, nil
}

// requireNoFloat walks raw JSON and rejects any number that is not an integer.
//
// §7.6: "no floats appear anywhere in an envelope, which sidesteps JCS's hardest corner
// entirely." RFC 8785 does specify a serialisation for doubles; this is stricter on
// purpose, because the shortest round-trip form of a double is precisely where two
// language runtimes differ, and cross-runtime byte equality is the whole point.
//
// Mirrors `backend/src/core/canonical.py::_reject_floats`, including naming the path, so
// the two runtimes refuse the same documents for the same stated reason.
func requireNoFloat(raw json.RawMessage) error {
	if len(raw) == 0 {
		return nil
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return fmt.Errorf("%w: args is not valid JSON: %v", ErrSchema, err)
	}
	return walkNoFloat(value, "args")
}

// requireSafeInteger refuses an integer RFC 8785 cannot serialise exactly.
//
// Kept separate from requireNoFloat so the two failure modes stay distinguishable: a float is
// a document that should never have been built, whereas an out-of-domain integer is usually a
// counter that grew past a limit nobody wrote down.
func requireSafeInteger(path string, value int64) error {
	if value > MaxSafeInteger || value < -MaxSafeInteger {
		return fmt.Errorf("%w: %s=%d is outside ±%d; RFC 8785 defines numbers as IEEE-754 "+
			"doubles, so a larger value cannot round-trip and the Python side refuses to "+
			"canonicalise it at all", ErrIntegerDomain, path, value, MaxSafeInteger)
	}
	return nil
}

func walkNoFloat(value any, path string) error {
	switch typed := value.(type) {
	case json.Number:
		text := typed.String()
		if strings.ContainsAny(text, ".eE") {
			return fmt.Errorf("%w: %s carries the non-integer number %s; design §7.6 forbids a float "+
				"in an envelope because the shortest round-trip form of a double is where two runtimes "+
				"disagree", ErrFloatValue, path, text)
		}
		// An integer literal inside `args` is subject to the same exact-domain bound as `seq`.
		// Parsed rather than length-checked: "0000000000000000009" is nineteen characters and
		// well inside the domain.
		parsed, err := typed.Int64()
		if err != nil {
			return fmt.Errorf("%w: %s carries the integer %s, which does not fit in an int64: %v",
				ErrIntegerDomain, path, text, err)
		}
		if err := requireSafeInteger(path, parsed); err != nil {
			return err
		}
	case float64:
		// Reached only if a caller decoded without UseNumber somewhere upstream.
		return fmt.Errorf("%w: %s was decoded as a float64", ErrFloatValue, path)
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			if err := walkNoFloat(typed[key], path+"."+key); err != nil {
				return err
			}
		}
	case []any:
		for index, item := range typed {
			if err := walkNoFloat(item, fmt.Sprintf("%s[%d]", path, index)); err != nil {
				return err
			}
		}
	}
	return nil
}

// jcs serialises value per RFC 8785.
//
// Written here rather than taken as a dependency for two reasons. The member set is
// closed and shallow apart from `args`, so the whole implementation is the object-key
// sort plus JSON string escaping — and the key sort is the one subtle part: RFC 8785
// sorts by UTF-16 code unit, not by code point, which differ for anything above the BMP.
// Taking a third-party canonicaliser into the agent would also add a supply-chain
// component to sign, SBOM and pin (§8.6) for about eighty lines of code.
func jcs(value any) ([]byte, error) {
	var out strings.Builder
	if err := writeJCS(&out, value); err != nil {
		return nil, err
	}
	return []byte(out.String()), nil
}

func writeJCS(out *strings.Builder, value any) error {
	switch typed := value.(type) {
	case nil:
		out.WriteString("null")
	case bool:
		if typed {
			out.WriteString("true")
		} else {
			out.WriteString("false")
		}
	case string:
		writeJCSString(out, typed)
	case json.Number:
		text := typed.String()
		if strings.ContainsAny(text, ".eE") {
			return fmt.Errorf("%w: %s reached the serialiser", ErrFloatValue, text)
		}
		out.WriteString(text)
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sortByUTF16(keys)
		out.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				out.WriteByte(',')
			}
			writeJCSString(out, key)
			out.WriteByte(':')
			if err := writeJCS(out, typed[key]); err != nil {
				return err
			}
		}
		out.WriteByte('}')
	case []any:
		out.WriteByte('[')
		for index, item := range typed {
			if index > 0 {
				out.WriteByte(',')
			}
			if err := writeJCS(out, item); err != nil {
				return err
			}
		}
		out.WriteByte(']')
	default:
		return fmt.Errorf("%w: %T is not a JSON value", ErrSchema, value)
	}
	return nil
}

// sortByUTF16 orders keys by UTF-16 code unit, which is what RFC 8785 §3.2.3 requires.
//
// Go's sort.Strings orders by byte, which equals code-point order for UTF-8. That agrees
// with UTF-16 order for everything in the Basic Multilingual Plane and DISAGREES above
// it: a supplementary character encodes as a surrogate pair beginning 0xD800–0xDBFF,
// which sorts *below* U+E000–U+FFFF in UTF-16 and *above* it in UTF-8. An emoji key
// beside a private-use key is enough to make two runtimes produce different bytes.
func sortByUTF16(keys []string) {
	sort.Slice(keys, func(i, j int) bool {
		return lessUTF16(keys[i], keys[j])
	})
}

func lessUTF16(a, b string) bool {
	ua, ub := utf16.Encode([]rune(a)), utf16.Encode([]rune(b))
	for index := 0; index < len(ua) && index < len(ub); index++ {
		if ua[index] != ub[index] {
			return ua[index] < ub[index]
		}
	}
	return len(ua) < len(ub)
}

// writeJCSString writes a JSON string using RFC 8785's minimal escaping.
//
// The escape set is exactly the one RFC 8785 §3.2.2.2 fixes: the two mandatory escapes,
// the five short forms, and \u00XX for the remaining control characters. Notably it does
// NOT escape non-ASCII — Go's encoding/json escapes U+2028, U+2029 and, with HTML
// escaping on, <, > and &, all of which would produce bytes no other JCS implementation
// agrees with.
func writeJCSString(out *strings.Builder, value string) {
	out.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		default:
			if r < 0x20 {
				out.WriteString(fmt.Sprintf(`\u%04x`, r))
			} else {
				out.WriteRune(r)
			}
		}
	}
	out.WriteByte('"')
}

// EncodeSignature renders a raw MAC as §7.6's base64url form.
//
// Unpadded, because §7.6 says base64url and the padding would be a second spelling of
// the same value — two spellings mean a constant-time comparison that can fail for a
// reason unrelated to the key.
func EncodeSignature(mac []byte) string {
	return base64.RawURLEncoding.EncodeToString(mac)
}

// DecodeSignature parses §7.6's base64url form, accepting padded input as well.
//
// Accepting padding on the way IN while never producing it on the way OUT is deliberate
// asymmetry: a peer that pads is interoperable, and this side still has one spelling.
//
// It also rejects NON-CANONICAL base64, which is a sharp edge worth stating. A 32-byte
// MAC is 256 bits and 43 base64url characters carry 258, so the final character has TWO
// bits that decode to nothing — and Go's decoder ignores them. Four distinct 43-character
// strings therefore decode to the same MAC (two free bits, four combinations), so a
// signature would have four valid spellings and "every single-byte mutation is rejected"
// would be false as written. The round-trip check below makes the encoding canonical:
// re-encoding the decoded bytes must reproduce the input.
func DecodeSignature(encoded string) ([]byte, error) {
	if encoded == "" {
		return nil, fmt.Errorf("%w: signature is empty", ErrSchema)
	}
	trimmed := strings.TrimRight(encoded, "=")
	decoded, err := base64.RawURLEncoding.DecodeString(trimmed)
	if err != nil {
		return nil, fmt.Errorf("%w: signature is not base64url: %v", ErrSchema, err)
	}
	if base64.RawURLEncoding.EncodeToString(decoded) != trimmed {
		return nil, fmt.Errorf("%w: signature is not canonical base64url; its trailing bits are "+
			"non-zero, which would give one MAC several valid spellings", ErrSchema)
	}
	return decoded, nil
}
