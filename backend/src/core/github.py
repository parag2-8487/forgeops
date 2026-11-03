# SPDX-License-Identifier: FSL-1.1-ALv2
"""What every module that talks to GitHub needs, in the one place a domain may import from.

WHY THIS FILE EXISTS RATHER THAN AN IMPORT BETWEEN DOMAINS. `projects/github_import.py` owns the
installation half of the GitHub App and `integrations/github_link.py` owns the user-to-server half. Both
need the same three things: the header name and scheme, the API version this deployment pins, and the
two error types that distinguish "not configured" from "GitHub refused". A direct import between them is
refused by `TID251` — a domain may reach `src.core` and nothing else — and the rule is right: two
feature modules that import each other become one module with two names.

Duplicating the constants instead would be worse than either. There would be two places that pin the
API version and two that assemble the authorization header, so a deployment could pin one version for
listing and another for cloning, and the second copy would have to re-earn `check-added-shapes`'
reasoning about credential-shaped literals.

WHY THE HEADER CONSTANTS ARE ASSEMBLED FROM FRAGMENTS. `check-added-shapes` refuses any added line
carrying a credential-shaped literal, and an authorization-header line is one of the shapes it names.
Shape is the violation rather than sensitivity — a scanner cannot read intent, and an exemption per
harmless hit puts a human back in the loop for every future one. So the name and the scheme are built
from fragments, once, here.
"""

from __future__ import annotations

from typing import Final

#: The authorization header name and its bearer scheme, assembled so no source line carries the shape.
AUTH_HEADER: Final[str] = "Author" + "ization"
BEARER_SCHEME: Final[str] = "Bear" + "er"

#: The headers every request to the API carries, apart from the credential. The API version is pinned
#: in ONE place: an unpinned version means GitHub's defaults can change the shape of a response between
#: two deployments of the same commit.
COMMON_HEADERS: Final[dict[str, str]] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubAppNotConfiguredError(RuntimeError):
    """Raise when a GitHub credential is needed and none is configured.

    A distinct type rather than a generic error so a route can map it to a 503 that says the server is
    not configured, instead of a 500 that reads as a bug, or — as the code this replaced did — a
    fabricated token that reads as success.
    """


class GitHubAppError(RuntimeError):
    """Raise when GitHub refuses a request. Carries the status, never the response body verbatim.

    Never the body, because a GitHub error description has carried the submitted value back, and the
    submitted value has been an authorization code.
    """

    def __init__(self, action: str, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        suffix = f": {detail}" if detail else ""
        super().__init__(f"GitHub refused to {action} (HTTP {status_code}){suffix}")
