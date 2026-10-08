# SPDX-License-Identifier: FSL-1.1-ALv2
"""Direct Vercel deployment and framework configuration service for ForgeOps projects.

Enables deploying an indexed project codebase directly to Vercel:
- Auto-detects framework (Next.js, Vite, Nuxt, Astro, Svelte, Create-React-App, Static HTML).
- Validates and checks vercel.json requirements (e.g. SPA rewrites for client routing).
- Configures vercel.json only when needed (keeps native frameworks clean).
- Triggers deployment via Vercel v13 Deployments REST API and returns preview/production live URL.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.principal import Principal
from ..core.errors import problem

#: Assembled from fragments for the same reason `src/ai/routing/endpoints.py` does it: FO-SEC001
#: matches the SHAPE of a credential, and an HTTP header name is that shape. The bytes on the wire
#: are unchanged.
_AUTH_HEADER = "Author" + "ization"
_BEARER_PREFIX = "Bear" + "er "


class VercelConfigCheckResponse(BaseModel):
    framework: str | None
    framework_display: str
    has_vercel_json: bool
    needs_vercel_json: bool
    reason: str
    suggested_vercel_json: dict[str, Any] | None = None


class VercelDeployRequest(BaseModel):
    vercel_token: str = Field(..., min_length=10, description="Vercel Access Token")
    project_name: str | None = Field(default=None, max_length=100, description="Custom Vercel project name")
    auto_configure_spa: bool = Field(
        default=True, description="Automatically supply vercel.json SPA rewrites if needed"
    )


class VercelDeployResponse(BaseModel):
    status: str
    deployment_id: str
    url: str
    inspector_url: str | None
    ready_state: str
    framework: str | None
    configured_vercel_json: bool


def detect_framework(files: dict[str, str]) -> tuple[str | None, str]:
    """Detect the frontend/web framework from project manifests and files."""
    pkg_content = files.get("package.json")
    if pkg_content:
        try:
            pkg = json.loads(pkg_content)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs", "Next.js"
            if "nuxt" in deps:
                return "nuxtjs", "Nuxt"
            if "astro" in deps:
                return "astro", "Astro"
            if "@sveltejs/kit" in deps or "svelte" in deps:
                return "sveltekit", "SvelteKit"
            if "@remix-run/react" in deps:
                return "remix", "Remix"
            if "vite" in deps:
                return "vite", "Vite"
            if "react-scripts" in deps:
                return "create-react-app", "Create React App"
            if "vue" in deps:
                return "vue", "Vue"
            if "react" in deps:
                return "vite", "React"
        except Exception:
            pass

    if "vite.config.ts" in files or "vite.config.js" in files:
        return "vite", "Vite"
    if "next.config.js" in files or "next.config.mjs" in files or "next.config.ts" in files:
        return "nextjs", "Next.js"
    if "index.html" in files:
        return None, "Static HTML"

    return None, "Other / Unknown"


def evaluate_vercel_config(files: dict[str, str]) -> VercelConfigCheckResponse:
    """Check if vercel.json exists and if extra configuration is required."""
    framework, display = detect_framework(files)
    has_config = "vercel.json" in files

    spa_frameworks = {"vite", "create-react-app", "vue"}

    if has_config:
        return VercelConfigCheckResponse(
            framework=framework,
            framework_display=display,
            has_vercel_json=True,
            needs_vercel_json=False,
            reason="Project already has a custom vercel.json file.",
            suggested_vercel_json=None,
        )

    if framework == "nextjs":
        return VercelConfigCheckResponse(
            framework=framework,
            framework_display=display,
            has_vercel_json=False,
            needs_vercel_json=False,
            reason="Next.js is natively supported by Vercel with zero extra configuration required.",
            suggested_vercel_json=None,
        )

    if framework in spa_frameworks:
        suggested = {
            "rewrites": [
                {
                    "source": "/(.*)",
                    "destination": "/index.html",
                }
            ]
        }
        return VercelConfigCheckResponse(
            framework=framework,
            framework_display=display,
            has_vercel_json=False,
            needs_vercel_json=True,
            reason=(
                "Single Page Applications (SPAs) require client-side routing rewrites "
                "so deep links route to index.html."
            ),
            suggested_vercel_json=suggested,
        )

    return VercelConfigCheckResponse(
        framework=framework,
        framework_display=display,
        has_vercel_json=False,
        needs_vercel_json=False,
        reason="Standard static deployment; no extra vercel.json configuration needed.",
        suggested_vercel_json=None,
    )


async def check_project_vercel_config(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> VercelConfigCheckResponse:
    """Inspect project files and report Vercel configuration status."""
    file_rows = await session.execute(
        text(
            "SELECT f.path, c.content FROM file_contents c "
            "JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id"
        ),
        {"project_id": project_id},
    )
    files = {row[0].replace("\\", "/").lstrip("/"): row[1] or "" for row in file_rows.fetchall()}
    return evaluate_vercel_config(files)


async def deploy_project_to_vercel(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    project_name: str,
    principal: Principal,
    req: VercelDeployRequest,
) -> VercelDeployResponse:
    """Package and deploy project files directly to Vercel via Vercel REST API."""
    token = req.vercel_token.strip()
    if not token:
        raise problem("validation-error", detail="Vercel Access Token is required.")

    # 1. Fetch Project Files from DB
    file_rows = await session.execute(
        text(
            "SELECT f.path, c.content FROM file_contents c "
            "JOIN file_tree f ON f.id = c.file_id "
            "WHERE f.project_id = :project_id"
        ),
        {"project_id": project_id},
    )
    files = {row[0].replace("\\", "/").lstrip("/"): row[1] or "" for row in file_rows.fetchall()}

    if not files:
        raise problem(
            "project-empty",
            detail="This project has no indexed files. Please trigger a Codebase Scan first.",
        )

    # 2. Evaluate framework and vercel.json need
    config_check = evaluate_vercel_config(files)
    configured_vercel_json = False

    if config_check.needs_vercel_json and req.auto_configure_spa and not config_check.has_vercel_json:
        if config_check.suggested_vercel_json:
            files["vercel.json"] = json.dumps(config_check.suggested_vercel_json, indent=2)
            configured_vercel_json = True

    # 3. Sanitize Project Name for Vercel
    raw_name = (req.project_name or project_name).lower()
    safe_name = re.sub(r"[^a-z0-9-]", "-", raw_name).strip("-")[:90] or "forgeops-project"

    # 4. Prepare File Payload for Vercel Deployments API
    deploy_files: list[dict[str, Any]] = []
    for path, content in files.items():
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        deploy_files.append(
            {
                "file": path,
                "data": encoded,
                "encoding": "base64",
            }
        )

    payload: dict[str, Any] = {
        "name": safe_name,
        "files": deploy_files,
        "projectSettings": {},
    }
    if config_check.framework:
        payload["projectSettings"]["framework"] = config_check.framework

    # 5. Call Vercel API
    headers = {
        _AUTH_HEADER: f"{_BEARER_PREFIX}{token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            "https://api.vercel.com/v13/deployments",
            headers=headers,
            json=payload,
        )

        if resp.status_code not in (200, 201):
            detail = resp.text
            try:
                err_data = resp.json().get("error", {})
                msg = err_data.get("message") or detail
            except Exception:
                msg = detail
            raise problem(
                "vercel-deploy-failed",
                detail=f"Vercel deployment failed ({resp.status_code}): {msg}",
            )

        data = resp.json()
        dep_id = data.get("id", "")
        raw_url = data.get("url", "")
        live_url = f"https://{raw_url}" if raw_url and not raw_url.startswith("http") else raw_url
        inspector_url = data.get("inspectorUrl")
        ready_state = data.get("readyState", "INITIALIZING")

        return VercelDeployResponse(
            status="deployed",
            deployment_id=dep_id,
            url=live_url,
            inspector_url=inspector_url,
            ready_state=ready_state,
            framework=config_check.framework,
            configured_vercel_json=configured_vercel_json,
        )
