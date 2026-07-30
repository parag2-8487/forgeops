# SPDX-License-Identifier: FSL-1.1-ALv2
"""Headless authentication against a real Authentik (design.md §8.3, D-54; task 6.3).

Why this file exists, and why it is only this file
--------------------------------------------------
Task 6.3 requires the `auth` job to exercise "the real code+PKCE flow", and OQ-28 resolves
the split as "`auth` covers the real code+PKCE flow and the RBAC matrix; `e2e` covers the
product journey". Everything except reaching an *authenticated session* was already
provable without a browser. This module supplies that one missing step, and D-54 records
why it is not Playwright: coupling an authorization gate to Authentik's UI markup means a
vendor restyle turns into a red required check with no product defect.

`POST /api/v3/flows/executor/{slug}/` is the same API Authentik's own LDAP and RADIUS
outposts drive. Two of its mechanics are undocumented and are what defeated the first
attempt, so they are stated here rather than rediscovered:

1. **A completed stage answers `302 Location: <the executor itself>`**, not with the next
   challenge. A client that does not follow redirects sees an empty body and concludes the
   stage failed.
2. **At 2026.5.6 the identification stage reports `password_fields: false`**, so the
   password is a *separate* stage. Posting `{"uid_field", "password"}` together is answered
   with `password: This field is required` against the stage that has no password field.

The whole browser leg is confined to this module on purpose (D-54's reversal cost):
swapping it for a real browser later changes no production code and no other test.

Credentials
-----------
Every value passed in is synthetic, self-labelling and assembled at runtime by the caller.
Nothing here holds or defaults a credential.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

#: Authentik's backend flow executor. Versioned by Authentik and published in its schema.
EXECUTOR_PATH: Final[str] = "/api/v3/flows/executor/{slug}/"

#: The default authentication flow every OAuth2 provider's authorization flow delegates to.
DEFAULT_AUTHENTICATION_FLOW: Final[str] = "default-authentication-flow"

#: The challenge component that means "the flow is finished, go here".
DONE: Final[str] = "xak-flow-redirect"

#: How many challenges to answer before giving up. A correctly configured default flow
#: needs three; the bound stops a misconfiguration from looping.
MAX_STAGES: Final[int] = 8


class AuthentikLoginError(AssertionError):
    """The flow executor asked for something this helper cannot supply.

    An `AssertionError` subclass so a failure reads as a test failure with the offending
    challenge named, rather than as an exception nobody expected. The message always
    carries the challenge `component`, because that is the one field that turns "login
    broke" into a diagnosis.
    """


def login(
    base_url: str,
    *,
    username: str,
    password: str,
    flow_slug: str = DEFAULT_AUTHENTICATION_FLOW,
) -> httpx.Cookies:
    """Drive Authentik's authentication flow to an authenticated session.

    Returns the cookie jar holding `authentik_session`. Presenting it to `/authorize`
    makes Authentik mint a real authorization code instead of redirecting into its login
    flow — which is the whole point.
    """
    endpoint = base_url.rstrip("/") + EXECUTOR_PATH.format(slug=flow_slug)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # `follow_redirects=True` is load-bearing, not tidiness — see mechanic 1 above.
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        challenge = _challenge(client.get(endpoint, params={"query": ""}, headers=headers))

        for _ in range(MAX_STAGES):
            component = challenge.get("component")
            if component == DONE:
                return client.cookies
            answer = _answer_for(component, challenge, username=username, password=password)
            challenge = _challenge(client.post(endpoint, params={"query": ""}, json=answer, headers=headers))

        raise AuthentikLoginError(
            f"the flow did not finish within {MAX_STAGES} stages; last component was {challenge.get('component')!r}"
        )


def _answer_for(
    component: object,
    challenge: dict[str, Any],
    *,
    username: str,
    password: str,
) -> dict[str, Any]:
    """The payload for one challenge, or a failure naming what was asked for."""
    if component == "ak-stage-identification":
        answer: dict[str, Any] = {"component": component, "uid_field": username}
        # Honour the stage's own declaration rather than assuming either shape: an
        # identification stage WITH `password_stage` set accepts the password here, and
        # one without it rejects the field. Both configurations are valid Authentik.
        if challenge.get("password_fields") is True:
            answer["password"] = password
        return answer
    if component == "ak-stage-password":
        return {"component": component, "password": password}
    if component == "ak-stage-user-login":
        return {"component": component}
    if component == "ak-stage-authenticator-validate":
        raise AuthentikLoginError(
            "Authentik asked for a second factor. The default authentication flow only "
            "does this when the user has an authenticator device or the validation stage "
            "is set to `not_configured_action: configure`; a test user created through "
            "the API has neither, so this means the flow was reconfigured. Component: "
            f"{component!r}"
        )
    raise AuthentikLoginError(
        f"unexpected challenge {component!r}: this helper answers identification, "
        f"password and user-login only. Full challenge keys: {sorted(challenge)}"
    )


def _challenge(response: httpx.Response) -> dict[str, Any]:
    """The challenge body, with a failure that names the status and the errors."""
    if response.status_code != 200:
        raise AuthentikLoginError(f"the flow executor answered {response.status_code}: {response.text[:300]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise AuthentikLoginError(f"the flow executor answered a non-JSON body: {response.text[:200]}") from exc
    if not isinstance(body, dict):
        raise AuthentikLoginError(f"the flow executor answered a non-object body: {body!r}")

    # `response_errors` is how the executor reports a rejected answer, and it arrives
    # with HTTP 200 beside the *same* challenge. Without this, a wrong password looks
    # like an infinite password stage and the loop above exhausts MAX_STAGES with a
    # message that names the wrong problem.
    errors = body.get("response_errors")
    if errors:
        raise AuthentikLoginError(f"stage {body.get('component')!r} rejected the answer: {errors}")
    return body


__all__ = ["DEFAULT_AUTHENTICATION_FLOW", "AuthentikLoginError", "login"]
