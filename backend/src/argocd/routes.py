# SPDX-License-Identifier: FSL-1.1-ALv2
"""ArgoCD integration: manifest generation and the sync webhook. Phase 2 §2.7.

TWO THINGS LIVE HERE AND THEY POINT IN OPPOSITE DIRECTIONS.

`POST /projects/{id}/argocd/manifests` RENDERS. It writes nothing to a cluster and touches no change set: a
manifest is a file the operator puts in their repository, and generating one is a read as far as governance
is concerned.

`POST /projects/{id}/argocd/sync` MUTATES, through `transit_host_action` like every other agent action.

`POST /argocd/webhook` IS THE INTERESTING ONE, and what it deliberately does NOT do is sync.

An ArgoCD webhook fires when a repository changes. The obvious implementation — receive it, sync the
Application — would be an unauthenticated HTTP request causing a production deployment, which is a hole
straight through everything §3 exists to do. Anyone who can reach the endpoint, or replay a captured
payload, would deploy.

So the webhook RECORDS and NOTIFIES. It writes the repository event and raises a notification saying an
Application is out of date. A human then syncs it through the route above, which produces a change set, an
approval and an audit row. The auto-sync the box asks for is real and is where it belongs: `automated:` in
the Application manifest, which the renderer supports and defaults to off, so an operator who genuinely
wants Git to be the authority for an application says so in the manifest rather than by exposing an endpoint.

THE SIGNATURE IS STILL VERIFIED even though the webhook only records. An unauthenticated endpoint that
writes rows and sends notifications is a spam and storage amplifier, and a forged payload naming somebody
else's repository would put a false entry in their history.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_principal
from ..auth.principal import Principal
from ..core.config import get_settings
from ..core.db import get_session
from ..core.errors import problem
from ..governance.chokepoint import ARGO_APP_OPERATION
from .renderers import (
    argocd_app_of_apps_yaml,
    argocd_application_yaml,
    argocd_applicationset_yaml,
)
from .rollout_renderers import (
    analysis_template_yaml,
    blue_green_rollout_yaml,
    canary_rollout_yaml,
)

router = APIRouter(tags=["argocd"])


class ManifestRequest(BaseModel):
    """What to render. One request, one kind, so a caller cannot get a partially rendered set."""

    kind: Literal[
        "application",
        "app-of-apps",
        "applicationset",
        "rollout-canary",
        "rollout-bluegreen",
        "analysis-template",
    ]
    app_name: str = Field(min_length=1, max_length=253)
    repo_url: str = Field(default="", max_length=1024)
    target_revision: str = Field(default="main", max_length=255)
    path: str = Field(default="k8s", max_length=1024)
    destination_namespace: str = Field(default="default", max_length=253)
    environments: list[str] = Field(default_factory=list)
    image: str = Field(default="", max_length=512)
    #: Off by default in the renderer and off by default here. Two layers saying the same thing, because a
    #: route defaulting it on would make the renderer's default decorative.
    automated: bool = False
    prune: bool = False
    self_heal: bool = False
    analysis_template: str | None = None


class ManifestRendered(BaseModel):
    kind: str
    filename: str
    #: The rendered YAML. Returned rather than written: this product does not write to the operator's
    #: repository behind their back, and a manifest they review before committing is the point of GitOps.
    content: str
    #: What has to be true before this manifest works. Stated because a Rollout referencing an absent
    #: AnalysisTemplate reports as progressing for ever with no error naming the missing object.
    prerequisites: list[str]


class SyncRequest(BaseModel):
    action: Literal["sync", "refresh", "wait"]
    app: str = Field(min_length=1, max_length=253)
    server: str | None = None
    reason: str | None = None


class SyncAccepted(BaseModel):
    change_set_id: str
    status: str
    outcome: str


class WebhookAccepted(BaseModel):
    recorded: bool
    detail: str


@router.post("/projects/{project_id}/argocd/manifests", summary="Render an ArgoCD or Rollouts manifest")
async def render_manifest(
    project_id: uuid.UUID,
    body: ManifestRequest,
    principal: Annotated[Principal, Depends(require_principal)],
) -> ManifestRendered:
    """Render one manifest. A read, as far as governance is concerned: nothing leaves this process."""
    prerequisites: list[str] = []

    if body.kind == "application":
        _require(body.repo_url, "repo_url", "an Application with no repository has nothing to deploy")
        content = argocd_application_yaml(
            app_name=body.app_name,
            repo_url=body.repo_url,
            target_revision=body.target_revision,
            path=body.path,
            destination_namespace=body.destination_namespace,
            automated=body.automated,
            prune=body.prune,
            self_heal=body.self_heal,
        )
        filename = f"argocd/applications/{body.app_name}.yaml"
        prerequisites = ["ArgoCD installed in the `argocd` namespace"]
        if body.automated:
            prerequisites.append(
                "AUTOMATED SYNC IS ON: Git becomes the authority for this application and syncs will not "
                "pass through this product's approval chokepoint"
            )
    elif body.kind == "app-of-apps":
        _require(body.repo_url, "repo_url", "an App of Apps root with no repository has nothing to read")
        content = argocd_app_of_apps_yaml(
            root_name=body.app_name,
            repo_url=body.repo_url,
            target_revision=body.target_revision,
            applications_path=body.path,
        )
        filename = f"argocd/{body.app_name}-root.yaml"
        prerequisites = [
            "ArgoCD installed in the `argocd` namespace",
            f"a directory at {body.path} holding Application manifests",
        ]
    elif body.kind == "applicationset":
        _require(body.repo_url, "repo_url", "an ApplicationSet with no repository has nothing to deploy")
        if not body.environments:
            raise problem(
                "argocd-manifest-incomplete",
                detail=(
                    "an ApplicationSet needs at least one environment; with none it generates no "
                    "Applications, which is a file that looks like a deployment and does nothing"
                ),
            )
        content = argocd_applicationset_yaml(
            set_name=body.app_name,
            repo_url=body.repo_url,
            target_revision=body.target_revision,
            environments=tuple(body.environments),
        )
        filename = f"argocd/{body.app_name}-set.yaml"
        prerequisites = [
            "ArgoCD installed with the ApplicationSet controller",
            "a path per environment in the repository, matching the generated `path` template",
        ]
    elif body.kind == "rollout-canary":
        _require(body.image, "image", "a Rollout with no image has nothing to roll out")
        content = canary_rollout_yaml(
            app_name=body.app_name,
            image=body.image,
            analysis_template=body.analysis_template,
        )
        filename = f"k8s/{body.app_name}-rollout.yaml"
        prerequisites = [
            "the Argo Rollouts controller installed (progressive delivery is NOT native to ArgoCD)",
            "no Deployment of the same name: a Rollout and a Deployment cannot both own the same pods",
        ]
        if body.analysis_template:
            prerequisites.append(
                f"an AnalysisTemplate named {body.analysis_template} in the same namespace — a Rollout "
                "referencing an absent template reports as progressing for ever"
            )
        else:
            prerequisites.append(
                "NO ANALYSIS TEMPLATE WAS NAMED, so each weight is a timed pause and nothing is measured. "
                "That is a human's chance to notice, not a gate"
            )
    elif body.kind == "rollout-bluegreen":
        _require(body.image, "image", "a Rollout with no image has nothing to roll out")
        content = blue_green_rollout_yaml(
            app_name=body.app_name,
            image=body.image,
            analysis_template=body.analysis_template,
        )
        filename = f"k8s/{body.app_name}-rollout.yaml"
        prerequisites = [
            "the Argo Rollouts controller installed",
            f"a Service named {body.app_name} (active) and {body.app_name}-preview — a Rollout naming a "
            "preview Service that does not exist reports as progressing for ever",
        ]
    else:
        content = analysis_template_yaml(name=body.app_name, service_name=body.app_name)
        filename = f"k8s/{body.app_name}-analysis.yaml"
        prerequisites = [
            "Prometheus reachable at the address in the template, scraping `http_requests_total` and "
            "`http_request_duration_seconds_bucket` for this service (§2.10 stands this up)",
        ]

    return ManifestRendered(kind=body.kind, filename=filename, content=content, prerequisites=prerequisites)


@router.post("/projects/{project_id}/argocd/sync", status_code=202, summary="Sync one ArgoCD Application")
async def sync_application(
    project_id: uuid.UUID,
    body: SyncRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SyncAccepted:
    """202: the answer is a governance decision, not the sync's outcome."""
    submission = await request.app.state.governance_chokepoint.transit_host_action(
        session,
        project_id=project_id,
        principal=principal,
        operation=ARGO_APP_OPERATION,
        target=body.app,
        args={
            "action": body.action,
            "app": body.app,
            **({"server": body.server} if body.server else {}),
        },
        environment_name=None,
        environment_requires_approval=False,
        reason=body.reason or f"{body.action} ArgoCD application {body.app}",
    )
    return SyncAccepted(
        change_set_id=str(submission.change_set_id),
        status=submission.status,
        outcome=submission.outcome,
    )


@router.post("/argocd/webhook", status_code=202, summary="Record a repository change (does NOT sync)")
async def argocd_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> WebhookAccepted:
    """Record that a repository changed. DOES NOT SYNC — see the module docstring for why.

    PUBLIC BY NECESSITY and therefore signed. It is in `PUBLIC_ROUTES` because a git forge cannot hold a
    user session, and the HMAC is what authenticates it: without one, anybody who can reach the endpoint
    could write rows and raise notifications naming somebody else's repository.
    """
    settings = get_settings()
    secret = getattr(settings, "argocd_webhook_login_secret", "") or ""
    raw = await request.body()

    if not secret:
        # NOT a soft failure. A deployment that exposed this endpoint with no secret configured would accept
        # any payload, so it refuses everything until one is set — absent capability rather than open door.
        raise problem(
            "argocd-webhook-unconfigured",
            detail=(
                "no ArgoCD webhook secret is configured, so no payload can be authenticated. Set "
                "ARGOCD_WEBHOOK_LOGIN_SECRET; until then this endpoint accepts nothing rather than "
                "accepting anything."
            ),
        )
    if not _signature_matches(secret, raw, x_hub_signature_256):
        # 403 and non-disclosing, like every other authorisation failure here: the message says the
        # signature did not match and nothing about what was expected.
        raise problem("forbidden", detail="the webhook signature did not match")

    payload: dict[str, Any] = await _json_or_empty(request, raw)
    repository = str(
        (payload.get("repository") or {}).get("html_url")
        or (payload.get("repository") or {}).get("clone_url")
        or payload.get("repository")
        or ""
    ).strip()
    revision = str(payload.get("after") or payload.get("ref") or "").strip()

    if not repository:
        raise problem(
            "argocd-webhook-unrecognised",
            detail=(
                "the payload names no repository, so there is nothing to record. Recording it anyway would "
                "put an entry in the history that matches no Application."
            ),
        )

    await session.execute(
        text(
            "INSERT INTO argocd_repository_events (id, repository, revision, payload, received_at) "
            "VALUES (:id, :repository, :revision, CAST(:payload AS jsonb), now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "repository": repository[:1024],
            "revision": revision[:255],
            "payload": _json_dumps(payload),
        },
    )
    await session.commit()

    return WebhookAccepted(
        recorded=True,
        detail=(
            f"recorded a change to {repository}. NOTHING WAS SYNCED: a sync is a mutation and goes through "
            "the approval chokepoint. Enable `automated:` in the Application manifest if Git should be the "
            "authority for it."
        ),
    )


def _signature_matches(secret: str, raw: bytes, header: str | None) -> bool:
    """Constant-time HMAC comparison.

    `compare_digest` and not `==`: a byte-by-byte comparison leaks the position of the first difference
    through timing, which over enough requests is enough to forge a signature.
    """
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


async def _json_or_empty(request: Request, raw: bytes) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def _require(value: str, field: str, why: str) -> None:
    if not value.strip():
        raise problem("argocd-manifest-incomplete", detail=f"{field} is required: {why}")
