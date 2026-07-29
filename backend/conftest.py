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

import sys
from pathlib import Path

# Put the backend root on sys.path so `src` is importable as a package.
sys.path.insert(0, str(Path(__file__).parent))
