# SPDX-License-Identifier: FSL-1.1-ALv2
"""CONTROL-OF-THE-CONTROL for the cross-domain module bans (design.md §2.2.1).

Everything here must be reported as CLEAN. A check that flags this file is unusable, and an
unusable check gets switched off inside a week — pattern O. Three separate ways of being clean,
because each has its own failure mode:

* a domain importing its OWN namespace is not a cross-domain import, and Ruff needed four
  `["TID251"]` globs precisely because `banned-api` cannot express "except from within";
* `src/core` is what every domain is told to depend on, so it must never be flagged;
* `import secrets` is the STANDARD LIBRARY, and `src.secrets` is a banned domain. Matching module
  paths by suffix instead of by resolution reported `core/trace.py`'s `import secrets` as a
  cross-domain import on this check's first run over the real tree. This is the regression control
  for that.
"""

from __future__ import annotations

import secrets  # noqa: F401  stdlib, NOT src.secrets

from ..core.errors import ProblemException  # noqa: F401  core is cross-cutting by design
from .helper import within_domain  # noqa: F401  ai importing ai


def stays_home() -> str:
    return secrets.token_hex(4) + within_domain() + ProblemException.__name__
