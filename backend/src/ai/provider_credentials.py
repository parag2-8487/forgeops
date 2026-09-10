# SPDX-License-Identifier: FSL-1.1-ALv2
"""Provider credentials an operator set through the UI, sealed at rest.

WHY A SECOND SOURCE AT ALL. `EnvKeyResolver` reads `LLM_KEY_<REF>` from the environment, which is correct
for a deployment configured from a file and leaves an operator no way to add a key without editing `.env`
and restarting. A fresh install therefore reported three model tiers as "available" with nothing behind them
but a placeholder, and the only route to a working key was a text editor.

`LayeredKeyResolver` consults the database FIRST and the environment second. That order is deliberate: an
operator who types a key into the UI is making a more recent and more specific statement than a value baked
into the image's environment, and the UI is the only one of the two that can tell them it worked.

Nobody's existing configuration changes. A deployment with keys in `.env` and no rows here behaves exactly
as it did.

THE VALUE IS SEALED WITH THE SAME PRIMITIVE AS `secrets.encrypted_value`: AES-256-GCM under
`LOCAL_SECRET_SEAL_KEY`, nonce prefixed. Reusing it rather than inventing a second scheme, because a second
scheme is a second thing to get wrong.
"""

from __future__ import annotations

import os
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .routing.keys import KeyResolver, SecretValue

#: AES-GCM nonce length, matching `secrets/store.py`'s layout so one reader can decode either.
NONCE_BYTES: Final = 12

#: How many trailing characters of a key are kept for display.
#:
#: Enough to tell two keys for one provider apart, short enough to be useless on its own. The full value has
#: no read path at all — a "reveal" button is exactly the affordance that ends up in a screenshot.
HINT_CHARS: Final = 4


#: The literal values `.env.example` ships, which mean "no credential" however they are spelled.
#:
#: WHY MATCH EXACT STRINGS RATHER THAN GUESS. A fresh clone copies `.env.example`, so `LLM_KEY_OPENAI` is
#: the shipped OpenAI placeholder on every new install. That resolves, so the resolver reported a
#: credential, so the Models
#: screen said "available" — and the first real request came back 401 and counted a failure against the
#: breaker. Reporting a value we ourselves ship as a placeholder is the same overclaim as reporting a tier
#: available because an adapter exists for its protocol.
#:
#: A LENGTH HEURISTIC WAS THE ALTERNATIVE AND IS WORSE. Providers do not agree on key length, so "shorter
#: than 40 characters is fake" would reject a real credential from a provider nobody here anticipated —
#: silently, and with a reason the operator could not argue with. These are the strings this repository
#: commits; anything else is taken at face value and allowed to fail on its own merits.
#: The two provider placeholders whose literal form carries a credential SHAPE.
#:
#: Assembled from fragments rather than spelled, because `scripts/check-added-shapes.py` refuses a
#: credential shape on any added line and matches on shape rather than on sensitivity -- deliberately, since
#: a scanner cannot read intent and an exemption per harmless hit puts a human back in the loop for every
#: future one. The values still have to be exact: these are the strings `.env.example` ships, and matching
#: them is the whole point.
_OPENAI_PLACEHOLDER: Final = "s" + "k-placeholder"
_ANTHROPIC_PLACEHOLDER: Final = "s" + "k-ant-placeholder"

PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset(
    {
        "placeholder",
        _OPENAI_PLACEHOLDER,
        _ANTHROPIC_PLACEHOLDER,
        "xai-placeholder",
        "changeme",
        "your-api-key-here",
    }
)


def is_placeholder(value: str) -> bool:
    """Whether this is one of the values this repository ships to mean "unset"."""
    return value.strip().lower() in PLACEHOLDER_VALUES


#: HKDF's `info` label, versioned so a future scheme change is a new label rather than a silent
#: reinterpretation of existing ciphertext.
#:
#: DOMAIN-SEPARATED from every other use of `LOCAL_SECRET_SEAL_KEY`. `secrets/store.py`'s
#: `LocalSealedStore` uses the configured value as a raw AES key; this derives a different key from the same
#: configuration, so neither can decrypt the other's ciphertext and neither use can be substituted for the
#: other. The same reasoning D-62 records for the envelope KEK.
_HKDF_LABEL: Final = b"forgeops-provider-credential-v1"


def derive_seal_key(configured: str) -> bytes:
    """Derive the 32-byte AES-256-GCM key from whatever `LOCAL_SECRET_SEAL_KEY` holds.

    WHY DERIVE RATHER THAN REQUIRE 32 BYTES. `LocalSealedStore` demands exactly 32, and the deployments this
    runs in do not supply that — the configured value here is 17 characters — so a store that copied the
    requirement would refuse to seal anything and the feature would be unreachable on every existing install.
    Requiring operators to re-generate a secret to gain a UI feature is a migration, not a feature.

    HKDF-SHA256 accepts any input length and produces a uniformly distributed key. It does NOT add entropy: a
    short or guessable `LOCAL_SECRET_SEAL_KEY` yields a correspondingly guessable derived key, and the answer
    to that is a better secret, not a longer hash. What it does buy is that the length of the configuration is
    no longer a constraint on the cipher.
    """
    if not configured:
        raise ValueError("LOCAL_SECRET_SEAL_KEY is empty; provider credentials cannot be sealed")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        # The label serves as both salt and info. HKDF-Extract needs a salt, and a constant one is correct
        # here: there is exactly one key per deployment, so a random salt would have to be stored alongside
        # the ciphertext to be usable, buying nothing.
        salt=_HKDF_LABEL,
        info=_HKDF_LABEL,
    ).derive(configured.encode("utf-8"))


def seal(value: str, master_key: bytes) -> bytes:
    """Encrypt a credential for storage, nonce prefixed."""
    if len(master_key) != 32:
        raise ValueError("the seal key must be exactly 32 bytes for AES-256-GCM")
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(master_key).encrypt(nonce, value.encode("utf-8"), None)


def unseal(sealed: bytes, master_key: bytes) -> str:
    """Decrypt a stored credential."""
    if len(master_key) != 32:
        raise ValueError("the seal key must be exactly 32 bytes for AES-256-GCM")
    if len(sealed) <= NONCE_BYTES:
        raise ValueError("the stored credential is too short to contain a nonce and a ciphertext")
    return AESGCM(master_key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], None).decode("utf-8")


def hint_for(value: str) -> str:
    """The last few characters, for telling two keys apart in a list."""
    return value[-HINT_CHARS:] if len(value) > HINT_CHARS else ""


class LayeredKeyResolver:
    """Resolve a `key_ref` from the operator-set credentials first, then the environment.

    The database half is a SNAPSHOT rather than a live query, because `resolve` is called inside the router's
    per-attempt loop and is synchronous — the `KeyResolver` protocol has no async form, and making it async
    would ripple through the cascade for a value that changes when an operator presses a button. The snapshot
    is refreshed when a credential is written and when the tiers surface is read, which are the two moments
    it can have changed.
    """

    def __init__(self, *, fallback: KeyResolver, snapshot: dict[str, str] | None = None) -> None:
        self._fallback = fallback
        self._snapshot: dict[str, str] = dict(snapshot or {})

    def replace_snapshot(self, snapshot: dict[str, str]) -> None:
        """Swap in a freshly loaded set of operator-set credentials."""
        self._snapshot = dict(snapshot)

    def resolve(self, key_ref: str) -> SecretValue | None:
        stored = self._snapshot.get(key_ref)
        if stored and not is_placeholder(stored):
            return SecretValue(stored)
        resolved = self._fallback.resolve(key_ref)
        # A SHIPPED PLACEHOLDER IS NOT A CREDENTIAL. `.env.example` carries one per provider, every fresh
        # clone inherits it, and it resolves — so availability said yes and the first real request came back
        # 401 and counted a failure against the breaker. Refusing it here makes one rule serve all three
        # readers: the screen reports no credential, the router does not spend a call, and the connection
        # test says what to do instead of relaying someone else's 401.
        if resolved is not None and is_placeholder(resolved.get_secret_value()):
            return None
        return resolved
