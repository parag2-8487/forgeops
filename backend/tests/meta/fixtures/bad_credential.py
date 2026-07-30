# SPDX-License-Identifier: FSL-1.1-ALv2
"""Fixture: every shape `check-test-credentials.py` must flag.

Never collected as a test — `backend/pyproject.toml` excludes `tests/meta/fixtures` from
collection, and the checker parses this file with `ast` rather than importing it.

Each value below is deliberately credential-SHAPED and just as deliberately worthless. That
is the whole point of the rule: a scanner cannot tell the difference, so shape alone is the
violation. The strings are built with concatenation so this fixture does not itself trip the
scanners it exists to describe — the checker sees the joined value because it evaluates
literals, not because the file contains one.
"""

from __future__ import annotations

# Split across `+` so this fixture does not itself put a contiguous credential-shaped string
# in the repository — and the checker folds constant concatenation, so splitting does NOT
# evade it. That is deliberate on both sides: `"Bea" + "rer …"` is the obvious way to slip a
# shape past a scanner, so the check has to see through it, and this file is the proof that it
# does. Payloads are long because every rule requires a token-shaped run: a scheme name alone
# is prose, and a check that fires on prose is a check people switch off.
JWT_SHAPED = "ey" + "JhbGciOiJub25lIn0.e30.AAAAAAAAAAAA"
BEARER_SHAPED = "Bea" + "rer abcdefghijklmnopqrstuvwxyz012345"
BASIC_SHAPED = "Ba" + "sic YWxpY2U6b3Blbi1zZXNhbWUtbG9uZ2VyLXBheWxvYWQ="
OPENAI_SHAPED = "sk" + "-abcdefghijklmnopqrstuvwx"
GITHUB_SHAPED = "gh" + "p_abcdefghijklmnopqrstuvwx"
AWS_SHAPED = "AK" + "IAABCDEFGHIJKLMN"
GOOGLE_SHAPED = "AI" + "zaAbCdEfGhIjKlMnOpQrStUvWxYz"
SLACK_SHAPED = "xo" + "xb-abcdefghijklmnopqr"
PEM_SHAPED = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5

# A suppression with no reason is itself a finding.
UNREASONED = "Bea" + "rer abcdefghijklmnopqrstuvwxyz012345"  # noqa: FO-SEC001
