# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent's session endpoint must mean the same thing everywhere it is written down.

WHAT THIS PROTECTS. `Settings.agent_session_ws_url` builds the address every freshly paired agent is
told to dial, and it builds it from a path constant declared in `core/config.py`. The route the
server actually registers is `WS_AGENT_PATH` in `websocket/routes.py`. Those are two spellings of one
fact, and configuration deliberately does not import from the web layer, so nothing but this compares
them. A drift would advertise a URL to every agent that resolves to nothing — and the failure would
surface as "the agent cannot connect", pointing at TLS or at the port rather than at a typo.

The published host port is checked too. `docker-compose.yml` interpolates `AGENT_TLS_HOST_PORT` for
the mapping and the backend reports the same variable in what it advertises; if the two disagreed,
the address handed out would name a port nothing is published on. That was the state before the
listener was added to the default stack at all.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yml"
E2E_COMPOSE = REPO_ROOT / "docker-compose.e2e.yml"
DOTENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_the_configured_agent_ws_path_is_the_route_the_server_registers() -> None:
    from src.core.config import AGENT_WS_PATH
    from src.websocket.routes import WS_AGENT_PATH

    assert AGENT_WS_PATH == WS_AGENT_PATH, (
        "the path used to build the advertised session URL differs from the route the server "
        "registers, so every agent would be told to dial an address that resolves to nothing"
    )


def test_the_session_url_is_always_tls() -> None:
    """A client certificate exists only inside a TLS handshake.

    The endpoint authenticates with one, so a `ws://` spelling of this address is not a
    configuration the deployment can have — the backend would refuse the session with "client
    certificate and bearer device token are both required". Asserted so no later edit makes it
    follow `agent_connect_tls`, which governs the BROWSER-facing port and is a different question.
    """
    from src.core.config import Settings

    url = Settings.agent_session_ws_url.fget(  # type: ignore[attr-defined]
        _Stub(agent_connect_host="", agent_tls_host_port=8443)
    )
    assert url.startswith("wss://"), url


class _Stub:
    """The two attributes the property reads, so this needs no environment."""

    def __init__(self, *, agent_connect_host: str, agent_tls_host_port: int) -> None:
        self.agent_connect_host = agent_connect_host
        self.agent_tls_host_port = agent_tls_host_port


def test_both_compose_files_publish_the_listener_on_the_same_variable() -> None:
    """One default in two files, so a host agent finds the listener whichever stack it faces.

    `docker-compose.e2e.yml` publishing a different port from `docker-compose.yml` would mean the
    address the backend advertises is right in one stack and wrong in the other.
    """
    pattern = re.compile(r"127\.0\.0\.1:\$\{AGENT_TLS_HOST_PORT:-(\d+)\}:8443")
    defaults = {}
    for path in (COMPOSE, E2E_COMPOSE):
        match = pattern.search(path.read_text(encoding="utf-8"))
        assert match is not None, f"{path.name} does not publish the agent listener"
        defaults[path.name] = match.group(1)
    assert len(set(defaults.values())) == 1, defaults


def test_the_host_port_variable_is_declared_in_the_example_environment() -> None:
    """Undeclared, it is invisible to anyone reading `.env.example` to see what can be set."""
    text = DOTENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^AGENT_TLS_HOST_PORT=\d+", text, re.MULTILINE), (
        "AGENT_TLS_HOST_PORT drives the published mapping and the advertised address; a reader must be able to find it"
    )


def test_the_container_bind_port_and_the_host_port_are_different_settings() -> None:
    """One name for both would move the listener's own bind port along with the mapping.

    `AGENT_TLS_PORT` is what the listener binds inside its container; `AGENT_TLS_HOST_PORT` is what
    the host publishes it as. Spelling them the same reads as a single knob and silently breaks
    whichever side the operator did not mean.
    """
    compose = COMPOSE.read_text(encoding="utf-8")
    assert 'AGENT_TLS_PORT: "8443"' in compose
    assert "${AGENT_TLS_HOST_PORT:-" in compose
    assert "${AGENT_TLS_PORT:-" not in compose, (
        "the host mapping must not interpolate AGENT_TLS_PORT: that variable is the container's "
        "bind port, and moving it would leave the published mapping pointing at nothing"
    )
