# SPDX-License-Identifier: FSL-1.1-ALv2
"""What an agent needs to connect, computed by the backend and rendered by the UI.

WHY THIS EXISTS

`pair` refuses to guess a backend URL, and that refusal is right: a device token is a bearer
credential, so an agent that invented an `https` host would hand one to whatever answered. The
defect was that the value existed nowhere a user could find it. The Pairing screen printed

    forgeops-agent pair --code ABC123

which fails with "no backend URL configured: pair needs --backend or AGENT_BACKEND_WSS_URL", and
the correct answer -- `ws://localhost:18000/api/v1/ws/agent` -- was derivable only from
`BACKEND_PORT` in the repository's `.env`, which the backend did not read and the UI never saw.

So the backend computes it from its OWN configuration and the UI renders what it is given. One
source, and the instructions cannot drift from the deployment.

WHY THE HOST IS NOT TAKEN FROM THE REQUEST

`Host` and `X-Forwarded-Host` are caller-supplied. Building connection instructions from them would
let anybody who can reach this endpoint choose the host the next agent is told to send its device
token to -- a credential-redirection primitive handed out for free. `agent_connect_host` is a
setting for that reason, and empty means localhost.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..auth.dependencies import require_principal
from ..auth.principal import Principal

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

#: The route an agent's WebSocket session uses. One definition on this side, matching
#: `agentWebSocketPath` in the agent's own discovery, so a rendered command and a discovered URL
#: cannot disagree about the path.
AGENT_WS_PATH: Final[str] = "/api/v1/ws/agent"

#: The shells the UI can render a command for, and the only ones.
#:
#: `powershell` is separate from `cmd` because the difference is not cosmetic: PowerShell does not
#: search the current directory, so `forgeops-agent.exe pair ...` fails there with
#: "not recognized as the name of a cmdlet" while the identical text works in cmd.exe. That single
#: distinction is what made the printed command unrunnable on Windows.
Shell = Literal["powershell", "cmd", "bash"]


class AgentConnectionInfo(BaseModel):
    """Everything needed to render a runnable connect command."""

    backend_ws_url: str = Field(
        description="The value for --backend. Computed from this deployment's own configuration.",
    )
    agent_ws_path: str = Field(description="The WebSocket route, for reference.")
    release_tag: str = Field(
        description="The agent release the UI should offer for download, or an empty string when "
        "this deployment pins none.",
    )
    download_base_url: str = Field(
        description="Where the signed per-platform archives live, or an empty string.",
    )


def _connection_info(request: Request) -> AgentConnectionInfo:
    settings = request.app.state.settings
    host = settings.agent_connect_host or "localhost"
    scheme = "wss" if settings.agent_connect_tls else "ws"
    return AgentConnectionInfo(
        backend_ws_url=f"{scheme}://{host}:{settings.backend_port}{AGENT_WS_PATH}",
        agent_ws_path=AGENT_WS_PATH,
        release_tag=settings.agent_release_tag,
        download_base_url=settings.agent_download_base_url,
    )


@router.get(
    "/connection-info",
    response_model=AgentConnectionInfo,
    summary="What an agent needs in order to connect to this backend",
)
async def get_connection_info(
    request: Request,
    _principal: Annotated[Principal, Depends(require_principal)],
) -> AgentConnectionInfo:
    """Report this deployment's agent connection details.

    Behind `require_principal` and not public. Nothing here is a secret -- a port and a release tag
    -- but it is a map of the deployment, and an unauthenticated endpoint that describes where the
    agent socket lives is a courtesy to a scanner. The UI is authenticated anyway, so there is no
    cost to requiring it.
    """
    return _connection_info(request)
