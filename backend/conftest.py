# SPDX-License-Identifier: FSL-1.1-ALv2
"""Root conftest for backend tests.

The backend is ONE package rooted at ``src``. Tests therefore import through the
``src.`` prefix (``src.mcp.auth``, ``src.core.errors``) so every module resolves
to exactly one identity.

Why that matters, concretely: ``src/main.py`` registers the RFC 9457 exception
handler for ``src.core.errors.ProblemException``. If a test imported the gateway
as top-level ``mcp.auth``, its ``ProblemException`` would be a *different class
object* loaded from a second copy of the module, and the registered handler would
silently fail to catch it. Adding only ``src`` to ``sys.path`` invites exactly
that split, so the repository root is what goes on the path.
"""

import os
import sys
from pathlib import Path

# Put the backend root on sys.path so `src` is importable as a package.
sys.path.insert(0, str(Path(__file__).parent))

# ── ENVELOPE_PEPPER for the test session ──────────────────────────────────────
#
# `Settings` refuses an empty `ENVELOPE_PEPPER` in every environment, not only production, because
# an empty one is not a missing credential but a broken one: HMAC-SHA256 under an empty key still
# computes, so device tokens and pairing codes would be stored unkeyed, and D-62's HKDF-derived
# key-encryption key would be identical in every deployment on earth. See
# `core/config.py::_require_envelope_pepper`.
#
# `Settings` also sets `env_file=None` on purpose, so it reads environment variables and explicit
# kwargs and never a file. Dozens of unit tests construct `Settings(database_url=..., redis_url=...)`
# to exercise something unrelated — dispatcher selection, tier configuration — and none of them
# should have to know about a pepper to do it. A DEVELOPER gets one from `.env`; the test session is
# an environment too, so it gets one here.
#
# WHY THIS DOES NOT WEAKEN THE CHECK. It supplies a value, it does not bypass the validator: an
# empty pepper is still refused, and `test_config_envelope_pepper.py` proves it by passing one
# explicitly. `setdefault`, so a real environment (CI, a developer's shell) always wins — this is a
# floor, not an override.
#
# The value is self-labelling rather than random: a reader who finds it in a log or a dump should be
# able to tell immediately that it is not a deployment's secret.
os.environ.setdefault("ENVELOPE_PEPPER", "test-session-pepper-not-a-deployment-value")
