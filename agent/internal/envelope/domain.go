// SPDX-License-Identifier: Apache-2.0

// The domain-separation seam of §7.6, in its own file.
//
// It is separated for the same reason `rollback.go` was extracted from `apply.go` (Q-01):
// `go build -overlay` replaces a whole file, and Q-14's negative control — "remove the
// domain-separation prefix on one side only" — has to be a readable diff rather than a copy of
// four hundred lines that rots on the first unrelated edit. Keeping the constants and the one
// concatenation together means the overlay changes exactly the thing the control describes.
package envelope

// Version is the only accepted value of an envelope's `v` member.
//
// A version member that is never checked is a version member that cannot be used to
// change anything later, so it is validated rather than merely carried.
const Version = "1"

// DomainPrefix and ApprovalDomainPrefix are the domain-separation strings from §7.6.
//
// The prefix is why a signature over an envelope can never be replayed as a signature
// over an approval response, even though the same per-device key signs both. Without
// it, "the bytes verified" and "the bytes meant what the reader thinks they meant" are
// different statements.
const (
	DomainPrefix         = "forgeops-envelope-v1"
	ApprovalDomainPrefix = "forgeops-approval-v1"
)

// SigningInput returns prefix || 0x00 || CanonicalBytes(e).
//
// The concatenation lives here rather than at each call site so the order is fixed in one
// place. Two call sites that concatenated in different orders would each verify their own
// signatures happily and reject the other's.
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
