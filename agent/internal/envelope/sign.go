// SPDX-License-Identifier: Apache-2.0

package envelope

import (
	"crypto/hmac"
	"crypto/sha256"
)

// Sign returns the base64url HMAC-SHA256 of e's signing input under key.
//
// Why the AGENT has a signer at all, when §2.2.1 says the control plane is the sole
// holder of the signing key: this function does not hold a key, it takes one. It exists
// so the cross-runtime fixture corpus (Q-14) can be produced and checked from the Go side
// against the same synthetic, self-labelling test key the Python side uses, and so
// `internal/session` can sign an `approval.response` with the ApprovalDomainPrefix — the
// one thing §7.6 has the agent sign.
//
// The security property in §2.2.2 is unaffected, because it is about who possesses the
// per-device key, not about who can compute an HMAC. Stating that here rather than
// leaving a reader to wonder whether this function is a hole.
func Sign(prefix string, e Envelope, key []byte) (string, error) {
	unsigned := e
	unsigned.Signature = ""
	input, err := SigningInput(prefix, unsigned)
	if err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(input)
	return EncodeSignature(mac.Sum(nil)), nil
}
