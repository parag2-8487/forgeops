# SPDX-License-Identifier: FSL-1.1-ALv2
"""Fixture: the shapes `check-test-credentials.py` must NOT flag.

Three categories, and each one matters for a different reason:

1. **Runtime assembly.** The sanctioned remedy. `synthetic_secrets` joins the fragments, so
   the code under test receives the exact bytes it needs while no source file holds a
   contiguous credential-shaped string.
2. **Prose.** A docstring may name a shape. Explaining why `eyJ`-prefixed literals are
   forbidden is exactly what a reader needs, and a check that forbade the explanation would
   push it out of the file — so docstrings are excluded by design, not by oversight.
3. **A reasoned suppression.** The escape hatch exists, and it costs a sentence.
"""

from __future__ import annotations

from tests import synthetic_secrets

# 1. Assembled at runtime. No literal here resembles anything.
BEARER = synthetic_secrets.bearer_clause()
BASIC = synthetic_secrets.basic_clause()
UNSIGNED = synthetic_secrets.unsigned_jwt()
MARKER = synthetic_secrets.SYNTHETIC_MARKER

# Self-labelling plaintext is fine: it matches no provider format.
PLAIN = "test-only-not-a-real-secret"

# A scheme name on its own is not a credential — there is nothing after it.
SCHEME_ONLY = "Bearer"

# 3. A reasoned suppression, for the rare case where the literal really is required.
DOCUMENTED = "Bea" + "rer abcdefghijklmnopqrstuvwxyz012345"  # noqa: FO-SEC001 — fixture proving a reasoned suppression is accepted


def explains_the_shape() -> str:
    """Docstrings may discuss shapes: an `eyJ`-prefixed JWT, an `sk-` key, `AKIA…`.

    None of that is a value the code uses, so none of it is a finding.
    """
    return MARKER
