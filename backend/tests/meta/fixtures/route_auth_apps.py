# SPDX-License-Identifier: FSL-1.1-ALv2
"""Negative and positive fixture apps for `scripts/check-route-auth.py`.

`bad_app()` serves a route that is neither public nor protected — the exact omission
the checker exists to catch. `good_app()` serves the same surface with the dependency
attached. `empty_app()` serves nothing, which must ALSO fail: a checker that passes on
an empty inventory reports success for a build that composed no routes at all, and
that is how a vacuous gate is born.

These are fixtures, not application code. They are deliberately minimal and do not
import `create_app`, so a defect in the real composition cannot make the checker's own
tests pass or fail for the wrong reason.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from src.auth.dependencies import require_principal, require_role
from src.auth.models import UserRole
from src.auth.principal import Principal


def _health(app: FastAPI) -> None:
    """The public surface both fixtures share, taken from §4.4."""

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}


def bad_app() -> FastAPI:
    """One unprotected, non-public route."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _health(app)

    @app.get("/api/v1/projects")
    async def list_projects() -> list[str]:
        return []

    return app


def good_app() -> FastAPI:
    """The same surface, protected."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _health(app)

    @app.get("/api/v1/projects")
    async def list_projects(principal: Principal = Depends(require_principal)) -> list[str]:
        return [principal.subject]

    @app.delete("/api/v1/projects/{project_id}")
    async def delete_project(
        project_id: str,
        principal: Principal = Depends(require_role(UserRole.ADMIN)),
    ) -> dict[str, str]:
        return {"deleted": project_id, "by": principal.subject}

    return app


def empty_app() -> FastAPI:
    """No routes at all. Must fail, not pass vacuously."""
    return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def stale_allowlist_app() -> FastAPI:
    """Serves none of the auth-flow paths `PUBLIC_ROUTES` names.

    A stale exemption is a route that was renamed while its public entry stayed behind,
    ready to apply to whatever takes the old path next. `good_app` has the same gap, so
    this fixture exists only to name the case explicitly in a test.
    """
    return good_app()
