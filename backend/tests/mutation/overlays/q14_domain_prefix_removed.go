// SPDX-License-Identifier: Apache-2.0

// MUTATION OVERLAY — Q-14's negative control. Not part of the build.
//
// This is `agent/internal/envelope/domain.go` with the envelope domain-separation prefix removed:
// the signing input becomes `0x00 || CanonicalBytes(e)` instead of
// `"forgeops-envelope-v1" || 0x00 || CanonicalBytes(e)`. `scripts/mutation-harness.py` swaps it in
// with `go build -overlay` for one run; nothing imports it.
//
// Removed on ONE side only, which is Appendix B's wording and the point of the control: the Python
// signer still prefixes, and the committed corpus in `agent/testdata/envelopes/*.json` carries
// signatures computed with the prefix. So the mutated build disagrees with the other runtime while
// remaining perfectly self-consistent — which is exactly the class of bug a single-runtime test
// cannot see.
//
// `ApprovalDomainPrefix` and `Version` are left alone so the diff is one string.
package envelope

// Version is the only accepted value of an envelope's `v` member.
const Version = "1"

// DomainPrefix is MUTATED: the domain-separation string of §7.6 is gone.
const (
	DomainPrefix         = ""
	ApprovalDomainPrefix = "forgeops-approval-v1"
)

// SigningInput returns prefix || 0x00 || CanonicalBytes(e), with the signature identical to the
// original so a change to it stops the mutated build compiling rather than silently ceasing to
// mutate anything.
func SigningInput(prefix string, e Envelope) ([]byte, error) {
	canonical, err := CanonicalBytes(e)
	if err != nil {
		return nil, err
	}
	out := make([]byte, 0, len(prefix)+1+len(canonical))
	out = append(out, prefix...)
	out = append(out, 0x00)
	out = append(out, canonical...)
	return out, nil
}
