# SPDX-License-Identifier: FSL-1.1-ALv2
"""Ollama Model Readiness and Bounded Warm-up Probe.

Separates:
- Availability inspection (/api/tags)
- Residency/memory inspection (/api/ps)
- Bounded model weight loading (/api/generate with keep_alive)
- Periodic heartbeat callback support during cold start to maintain worker fencing leases
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import logging
from typing import Any
import httpx

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Base error for Ollama readiness and inference operations."""


class OllamaModelNotFoundError(OllamaError):
    """Configured model is not installed on the Ollama server."""


class OllamaWarmupTimeoutError(OllamaError):
    """Model cold loading timed out before weights finished loading."""


class OllamaInferenceTimeoutError(OllamaError):
    """Model inference timed out during generation."""


class OllamaClientCancelledError(OllamaError):
    """Operation was cancelled by client / operator."""


class OllamaUnavailableError(OllamaError):
    """Ollama server connection failed or server is unreachable."""


def _match_model_tag(target: str, candidate: str) -> bool:
    """Matches a target model name against an Ollama candidate tag."""
    t = target.strip().lower()
    c = candidate.strip().lower()
    if t == c:
        return True
    # Compare with and without :latest
    t_base = t.split(":", 1)[0]
    c_base = c.split(":", 1)[0]
    if t.endswith(":latest") and t_base == c:
        return True
    if c.endswith(":latest") and c_base == t:
        return True
    return False


async def check_ollama_model_available(
    base_url: str,
    model: str,
    *,
    timeout: float = 10.0,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Verifies that the configured model exists in the Ollama model store."""
    clean_base = base_url.rstrip("/").removesuffix("/v1")
    url = f"{clean_base}/api/tags"

    async def _fetch(client: httpx.AsyncClient) -> bool:
        resp = await client.get(url)
        if resp.status_code == 404:
            return True
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except Exception:
            return True
        if not isinstance(data, dict) or "models" not in data:
            return True
        models = data.get("models") or []
        for m in models:
            name = str(m.get("name") or "")
            model_tag = str(m.get("model") or "")
            if _match_model_tag(model, name) or _match_model_tag(model, model_tag):
                return True
        return False

    try:
        if http_client is not None:
            return await _fetch(http_client)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await _fetch(client)
    except (httpx.TimeoutException, asyncio.CancelledError):
        raise
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        raise OllamaUnavailableError(f"Failed to connect to Ollama daemon at {clean_base}: {exc}") from exc
    except Exception as exc:
        logger.warning("Error checking Ollama model availability on %s: %s", url, exc)
        return False


async def is_ollama_model_resident(
    base_url: str,
    model: str,
    *,
    timeout: float = 5.0,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Checks whether the model is currently resident in Ollama GPU/CPU memory."""
    clean_base = base_url.rstrip("/").removesuffix("/v1")
    url = f"{clean_base}/api/ps"

    async def _fetch(client: httpx.AsyncClient) -> bool:
        resp = await client.get(url)
        if resp.status_code == 404:
            return True
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
        except Exception:
            return True
        if not isinstance(data, dict) or "models" not in data:
            return True
        models = data.get("models") or []
        for m in models:
            name = str(m.get("name") or "")
            model_tag = str(m.get("model") or "")
            if _match_model_tag(model, name) or _match_model_tag(model, model_tag):
                return True
        return False

    try:
        if http_client is not None:
            return await _fetch(http_client)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await _fetch(client)
    except (httpx.TimeoutException, asyncio.CancelledError):
        raise
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        raise OllamaUnavailableError(f"Failed to connect to Ollama daemon at {clean_base}: {exc}") from exc
    except Exception as exc:
        logger.warning("Error checking Ollama model memory residency on %s: %s", url, exc)
        return False


async def ensure_ollama_model_ready(
    base_url: str,
    model: str,
    *,
    warmup_timeout_seconds: float = 360.0,
    poll_interval: float = 5.0,
    heartbeat_callback: Callable[[], Awaitable[None]] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Ensures that the model is loaded into memory before production inference is dispatched.

    Separates:
    - Model existence verification.
    - Cold-start weight loading with bounded timeout.
    - Periodic heartbeat execution to maintain active worker lease fencing.
    """
    clean_base = base_url.rstrip("/").removesuffix("/v1")

    # 1. Check availability
    available = await check_ollama_model_available(clean_base, model, timeout=10.0, http_client=http_client)
    if not available:
        raise OllamaModelNotFoundError(
            f"Configured Ollama model '{model}' is not installed on server '{clean_base}'. "
            "Model weights must be pulled before running generation."
        )

    # 2. Check if already resident in memory
    resident = await is_ollama_model_resident(clean_base, model, timeout=5.0, http_client=http_client)
    if resident:
        logger.info("Ollama model '%s' is already resident and warm in memory.", model)
        return

    logger.info(
        "Ollama model '%s' is cold. Initiating bounded warm-up (timeout=%ss)...",
        model,
        warmup_timeout_seconds,
    )

    # 3. Trigger weight loading via /api/generate with keep_alive
    generate_url = f"{clean_base}/api/generate"
    payload: dict[str, Any] = {
        "model": model,
        "prompt": "",
        "keep_alive": "10m",
    }

    async def _execute_warmup(client: httpx.AsyncClient) -> None:
        warmup_task = asyncio.create_task(client.post(generate_url, json=payload))

        while not warmup_task.done():
            done, _ = await asyncio.wait({warmup_task}, timeout=poll_interval)
            if done:
                break
            if heartbeat_callback is not None:
                with contextlib.suppress(Exception):
                    await heartbeat_callback()

        try:
            resp = await warmup_task
            if resp.status_code >= 400:
                raise OllamaUnavailableError(
                    f"Ollama warm-up request failed with HTTP {resp.status_code}: {resp.text[:200]}"
                )
            logger.info("Ollama model '%s' successfully warmed up and resident in memory.", model)
        except httpx.TimeoutException as exc:
            logger.warning(
                "Ollama model '%s' warm-up timed out after %ss: %s",
                model,
                warmup_timeout_seconds,
                exc,
            )
            raise OllamaWarmupTimeoutError(
                f"Ollama model '{model}' cold load timed out after {warmup_timeout_seconds}s "
                "loading weights into memory."
            ) from exc
        except asyncio.CancelledError:
            logger.info("Ollama model '%s' warm-up cancelled by caller.", model)
            raise OllamaClientCancelledError(
                f"Ollama warm-up for model '{model}' was cancelled."
            ) from None
        except Exception as exc:
            if isinstance(exc, OllamaError):
                raise
            raise OllamaUnavailableError(
                f"Ollama model '{model}' warm-up encountered connection error: {exc}"
            ) from exc

    if http_client is not None:
        await _execute_warmup(http_client)
    else:
        async with httpx.AsyncClient(timeout=warmup_timeout_seconds) as client:
            await _execute_warmup(client)

