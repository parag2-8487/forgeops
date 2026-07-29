# SPDX-License-Identifier: FSL-1.1-ALv2
"""MCP Gateway HTTP surface (Design §5.2, §11.4–§11.6).

Routes exposed here, all under the versioned API prefix:

    POST /api/v1/mcp                  Gateway ingress, routed by headers only
    GET  /api/v1/mcp/servers          Registry introspection, OPA-filtered
    GET  /api/v1/mcp/apps/{name}      MCP App descriptor + sandboxed host page

Routing is a pure function of ``Mcp-Method`` + ``Mcp-Name`` (P-05): the handler
below hands the gateway the *headers*, never the parsed body, and the gateway
verifies the bearer token before routing so an unauthenticated caller cannot use
the registry as an oracle.

The collaborators are read off ``request.app.state`` rather than constructed
here, so tests can build an app with substituted collaborators (Design §7.5).
Absent state means the gateway was not configured, which is a 503 — never a
silent unauthenticated pass-through.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, ORJSONResponse

from ..core.errors import ProblemException
from .apps import CSP_POLICY, SANDBOX_ATTRS, McpAppRegistry
from .routing import MCP_METHOD_HEADER, MCP_NAME_HEADER
from .tasks import TaskConflictError

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _require(request: Request, attr: str) -> Any:
    """Read a required collaborator off app.state or fail closed with 503."""
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise ProblemException(
            status=503,
            type_suffix="mcp-gateway-unconfigured",
            title="MCP gateway unavailable",
            detail="The MCP gateway is not configured on this instance.",
        )
    return value


@router.post("", response_class=ORJSONResponse)
async def mcp_ingress(request: Request) -> Response:
    """Single MCP ingress. Dispatch is chosen from headers, never from the body.

    Two orchestration paths exist and share no generic "forward then authorise"
    helper (Design §5.3):

      tools/list  verify → route → cache|upstream → OPA filter → return
      tools/call  verify → route → parse → resolve metadata → OPA authorise
                  → invoke upstream only on allow
    """
    gateway = _require(request, "mcp_gateway")

    # Header lookup is case-insensitive per HTTP; Starlette's Headers handles that.
    headers = {
        MCP_METHOD_HEADER: request.headers.get(MCP_METHOD_HEADER, ""),
        MCP_NAME_HEADER: request.headers.get(MCP_NAME_HEADER, ""),
    }
    authorization = request.headers.get("Authorization")
    method = headers[MCP_METHOD_HEADER].strip()

    if method == "tools/list":
        # The body is deliberately NOT read on this path.
        result = await gateway.handle_tools_list(authorization=authorization, headers=headers)
        return ORJSONResponse(result, headers=_trace_response(request))

    if method == "tools/call":
        body = await request.body()
        result = await gateway.handle_tools_call(authorization=authorization, headers=headers, body=body)
        return ORJSONResponse(result, headers=_trace_response(request))

    if method.startswith("tasks/"):
        return await _handle_tasks(request, gateway, method, authorization, headers)

    # Unknown or absent method: the router owns the error shape so that a missing
    # header is a 400 and an unknown server is a 404, never a default route.
    if not method:
        raise ProblemException(
            status=400,
            type_suffix="mcp-missing-routing-headers",
            title="Missing MCP routing headers",
            detail=f"Both {MCP_METHOD_HEADER} and {MCP_NAME_HEADER} are required.",
        )
    raise ProblemException(
        status=400,
        type_suffix="mcp-unsupported-method",
        title="Unsupported MCP method",
        detail=f"Method '{method}' is not supported by this gateway.",
    )


async def _handle_tasks(
    request: Request,
    gateway: Any,
    method: str,
    authorization: str | None,
    headers: dict[str, str],
) -> Response:
    """Tasks Extension lifecycle: tasks/get, tasks/update, tasks/cancel.

    Authentication runs first, exactly as on the tool paths. Cancellation of an
    already-terminal task returns that state with HTTP 200 and does not error
    (Design §11.5).
    """
    verifier = _require(request, "mcp_verifier")
    store = _require(request, "mcp_task_store")

    await verifier.verify(authorization)

    payload: dict[str, Any] = {}
    raw = await request.body()
    if raw:
        import json

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError as exc:
            raise ProblemException(
                status=400,
                type_suffix="mcp-invalid-body",
                title="Invalid request body",
                detail="Request body is not valid JSON.",
            ) from exc

    params = payload.get("params") or {}
    task_id = params.get("id") or params.get("taskId")

    if method == "tasks/create":
        record = await store.create(kind=params.get("kind", "generic"), owner="default")
        return ORJSONResponse(_record_payload(record), headers=_trace_response(request))

    if not task_id:
        raise ProblemException(
            status=400,
            type_suffix="mcp-missing-task-id",
            title="Missing task id",
            detail="params.id is required for this tasks method.",
        )

    try:
        if method == "tasks/get":
            record = await store.get(task_id)
        elif method == "tasks/cancel":
            # Cancelling an already-terminal task returns that state with HTTP 200
            # and does not error: cancellation is idempotent (Design §11.5).
            record = await store.cancel(task_id)
        elif method == "tasks/update":
            state = params.get("state")
            if not state:
                raise ProblemException(
                    status=400,
                    type_suffix="mcp-missing-task-state",
                    title="Missing task state",
                    detail="params.state is required for tasks/update.",
                )
            record = await store.update(task_id, state)
        else:
            raise ProblemException(
                status=400,
                type_suffix="mcp-unsupported-method",
                title="Unsupported MCP method",
                detail=f"Method '{method}' is not supported by this gateway.",
            )
    except TaskConflictError as exc:
        # Two writers raced; the compare-and-set loser is told so explicitly
        # rather than silently overwriting the winner (P-10).
        raise ProblemException(
            status=409,
            type_suffix="mcp-task-conflict",
            title="Task state conflict",
            detail="The task changed state concurrently; re-read it and retry.",
        ) from exc
    except ValueError as exc:
        if "not found" in str(exc):
            raise ProblemException(
                status=404,
                type_suffix="mcp-unknown-task",
                title="Unknown task",
                detail=f"No task with id '{task_id}'.",
            ) from exc
        raise ProblemException(
            status=400,
            type_suffix="mcp-invalid-task-transition",
            title="Invalid task transition",
            detail=str(exc),
        ) from exc

    if record is None:
        raise ProblemException(
            status=404,
            type_suffix="mcp-unknown-task",
            title="Unknown task",
            detail=f"No task with id '{task_id}'.",
        )

    return ORJSONResponse(_record_payload(record), headers=_trace_response(request))


def _record_payload(record: Any) -> dict[str, Any]:
    """Normalise a TaskRecord to a JSON-serialisable mapping."""
    import json as _json

    if hasattr(record, "to_json"):
        return _json.loads(record.to_json())
    if hasattr(record, "to_dict"):
        return record.to_dict()
    return dict(record)


@router.get("/servers", response_class=ORJSONResponse)
async def list_servers(request: Request) -> Response:
    """Registry introspection. Requires a verified bearer token like every other
    gateway surface, so the registry cannot be enumerated anonymously."""
    verifier = _require(request, "mcp_verifier")
    registry = _require(request, "mcp_registry")

    await verifier.verify(request.headers.get("Authorization"))

    servers = [
        {
            "name": d.name,
            "description": d.description,
            "capabilities": list(d.capabilities),
        }
        for d in registry.all().values()
    ]
    return ORJSONResponse({"servers": servers}, headers=_trace_response(request))


@router.get("/apps/{name}", response_class=ORJSONResponse)
async def get_app_descriptor(name: str, request: Request) -> Response:
    """MCP App descriptor. The host page is served separately at /apps/{name}/host."""
    registry: McpAppRegistry = getattr(request.app.state, "mcp_app_registry", None) or McpAppRegistry()

    descriptor = registry.get(name)
    if descriptor is None:
        raise ProblemException(
            status=404,
            type_suffix="mcp-unknown-app",
            title="Unknown MCP app",
            detail=f"No MCP app named '{name}'.",
        )

    headers = _trace_response(request)
    headers["Content-Security-Policy"] = CSP_POLICY
    return ORJSONResponse(descriptor.to_dict(), headers=headers)


@router.get("/apps/{name}/host", response_class=HTMLResponse)
async def get_app_host_page(name: str, request: Request) -> Response:
    """Sandboxed iframe host page.

    The iframe carries ``allow-scripts allow-forms`` and deliberately NOT
    ``allow-same-origin``, so the app cannot reach the parent origin's storage or
    cookies even though it executes script.
    """
    registry: McpAppRegistry = getattr(request.app.state, "mcp_app_registry", None) or McpAppRegistry()

    descriptor = registry.get(name)
    if descriptor is None:
        raise ProblemException(
            status=404,
            type_suffix="mcp-unknown-app",
            title="Unknown MCP app",
            detail=f"No MCP app named '{name}'.",
        )

    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{descriptor.title}</title></head><body>"
        f'<iframe src="{descriptor.entry_url}" sandbox="{SANDBOX_ATTRS}" '
        f'title="{descriptor.title}" style="border:0;width:100%;height:100%"></iframe>'
        "</body></html>"
    )

    headers = _trace_response(request)
    headers["Content-Security-Policy"] = CSP_POLICY
    return HTMLResponse(content=html, headers=headers)


def _trace_response(request: Request) -> dict[str, str]:
    """Echo the W3C traceresponse header when a trace context is in play."""
    traceresponse = getattr(request.state, "traceresponse", None)
    return {"traceresponse": traceresponse} if traceresponse else {}
