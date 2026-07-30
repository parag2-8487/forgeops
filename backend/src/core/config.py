# SPDX-License-Identifier: FSL-1.1-ALv2
"""Strict project configuration loading (design.md §7.1, §13.1).

Settings uses pydantic-settings with extra=forbid and env_file=None. Unknown keys
from PROJECT SOURCES (.env.example, .env, explicit mappings) are errors accumulated
into ONE report. Arbitrary ambient OS env vars (PATH, HOME, CI, editor vars) must
be ignored and must never cause failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The complete Phase 0 .env.example key inventory (74 keys from design.md §13.1).
# Used to validate project sources — only these keys are allowed.
PROJECT_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        # Core
        "APP_ENV",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "SERVICE_VERSION",
        "GIT_COMMIT",
        # PostgreSQL
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_PORT",
        "DATABASE_URL",
        "DATABASE_POOL_SIZE",
        "PGVECTOR_HNSW_EF_SEARCH",
        # Redis
        "REDIS_URL",
        "REDIS_PORT",
        "REDIS_SEMANTIC_INDEX",
        # Backend HTTP
        "BACKEND_PORT",
        "FRONTEND_PORT",
        "OPA_PORT",
        "API_PREFIX",
        "CORS_ALLOW_ORIGINS",
        # MCP Gateway
        "MCP_OIDC_ISSUERS",
        "MCP_OIDC_AUDIENCE",
        "MCP_OIDC_JWKS_TTL_SECONDS",
        "MCP_CACHE_MAX_TTL_MS",
        "MCP_SERVER_REGISTRY_PATH",
        "MCP_AGENT_BLAST_RADIUS",
        "OPA_URL",
        # Model routing
        "MODEL_TIER_CONFIG_PATH",
        "CB_FAILURE_THRESHOLD",
        "CB_WINDOW_SECONDS",
        "CB_OPEN_SECONDS",
        "SEMANTIC_CACHE_THRESHOLD",
        "SEMANTIC_CACHE_TTL_SECONDS",
        "EMBEDDING_MODEL_ID",
        "EMBEDDING_DIMS",
        "AI_RATE_LIMIT_CAPACITY",
        "AI_RATE_LIMIT_REFILL_PER_SECOND",
        "AI_RATE_LIMIT_FAIL_MODE",
        "OUTBOUND_HTTP_TIMEOUT_SECONDS",
        # BYO-Key
        "LLM_KEY_RESOLVER",
        "LLM_KEY_OPENAI",
        "LLM_KEY_ANTHROPIC",
        "LLM_KEY_XAI",
        "LLM_KEY_GOOGLE",
        "LLM_KEY_DEEPSEEK",
        "OPENAI_BASE_URL",
        "XAI_BASE_URL",
        "DEEPSEEK_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "GOOGLE_BASE_URL",
        "SELF_HOSTED_BASE_URL",
        "INFISICAL_URL",
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
        "INFISICAL_PROJECT_ID",
        # GitOps
        "GITHUB_TOKEN",
        "GITHUB_API_BASE_URL",
        "GITHUB_REPO",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_BRANCH_PREFIX",
        "GIT_PR_POLL_INTERVAL_SECONDS",
        "GIT_PR_POLL_TIMEOUT_SECONDS",
        # OpenTofu
        "TOFU_BINARY",
        "TOFU_VERSION",
        "TOFU_TIMEOUT_SECONDS",
        "TOFU_KILL_GRACE_SECONDS",
        "TF_PLUGIN_CACHE_DIR",
        # Agent
        "AGENT_BACKEND_WSS_URL",
        "AGENT_SHUTDOWN_TIMEOUT_SECONDS",
        "AGENT_MCP_TRANSPORT",
        # Telemetry
        "TRACE_PROPAGATION_ENABLED",
        # Frontend
        "NEXT_PUBLIC_API_BASE_URL",
        "NEXT_PUBLIC_APP_NAME",
    }
)


class Settings(BaseSettings):
    """Backend settings validated from environment. extra=forbid rejects unknown keys
    when constructing from explicit project mappings. env_file=None prevents pydantic
    from auto-loading dotenv files (we do that explicitly in load_project_dotenv).
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="forbid",
        case_sensitive=False,
    )

    app_env: str = Field(default="development", pattern=r"^(development|test|production)$")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="console")
    api_prefix: str = Field(default="/api/v1")
    service_version: str = Field(default="0.0.0")
    git_commit: str = Field(default="unknown")

    database_url: PostgresDsn
    database_pool_size: int = Field(default=10, ge=1, le=100)
    redis_url: RedisDsn

    # MCP Gateway
    mcp_oidc_issuers: str = Field(default="")
    mcp_oidc_audience: str = Field(default="forgeops-mcp-gateway")
    mcp_oidc_jwks_ttl_seconds: int = Field(default=600, ge=60)
    mcp_cache_max_ttl_ms: int = Field(default=300_000, ge=0)
    mcp_server_registry_path: str = Field(default="config/mcp-servers.yaml")
    # Phase 0 reads the blast radius from configuration; Phase 1 derives it from
    # the attested agent identity. The Rego policy is written against the input
    # field either way, so no policy change is needed later (OQ-20).
    mcp_agent_blast_radius: Literal["read_only", "workspace", "infrastructure"] = "read_only"
    opa_url: AnyHttpUrl = Field(default="http://opa:8181")  # type: ignore[assignment]

    # Model routing
    model_tier_config_path: str = Field(default="config/model-tiers.yaml")
    cb_failure_threshold: int = Field(default=5, ge=1)
    cb_window_seconds: int = Field(default=30, ge=1)
    cb_open_seconds: int = Field(default=60, ge=1)
    semantic_cache_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    outbound_http_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    ai_rate_limit_capacity: int = Field(default=20, ge=1)
    ai_rate_limit_refill_per_second: float = Field(default=0.2, gt=0)
    ai_rate_limit_fail_mode: Literal["fail_closed"] = "fail_closed"

    # CORS
    cors_allow_origins: str = Field(default="http://localhost:3000")

    @field_validator("mcp_oidc_issuers")
    @classmethod
    def _require_issuer_in_production(cls, v: str, info: Any) -> str:
        """An empty issuer allowlist in production would accept nothing — refuse to boot."""
        if not v.strip() and info.data.get("app_env") == "production":
            raise ValueError("MCP_OIDC_ISSUERS must be non-empty when APP_ENV=production")
        return v

    @property
    def mcp_oidc_issuer_list(self) -> list[str]:
        """The issuer allowlist as an exact-match list.

        Stored as a comma-separated string because that is what a 12-factor env
        var carries; exposed as a list so the verifier compares exact values and
        never does prefix matching.
        """
        return [i.strip() for i in self.mcp_oidc_issuers.split(",") if i.strip()]


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Handles comments, blank lines, and quoting."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Handle inline comments (only if not inside quotes)
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Strip inline comment
        value = value.split("#")[0].strip() if "#" in value and not value.startswith('"') else value.strip()
        # Strip quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def load_project_dotenv(
    files: Sequence[str | Path] = (".env.example", ".env"),
    *,
    base_dir: Path | None = None,
) -> dict[str, str]:
    """Parse project dotenv files and validate all keys are in PROJECT_CONFIG_KEYS.

    The first file (.env.example) is required; subsequent files are optional overrides.
    Unknown keys from project sources are accumulated into ONE error report.
    """
    if base_dir is None:
        # The repository root, which is the parent of backend/ and where the
        # committed `.env.example` lives.
        #
        # This said `parents[2]` and resolved to backend/ itself, contradicting its
        # own comment. Nothing caught it because every caller was a test passing an
        # explicit base_dir; the first production-shaped caller
        # (tests/integration/production_app.py, once debt D1 made the tier YAML
        # load for real) hit `FileNotFoundError` immediately.
        # config.py -> core -> src -> backend -> <repo root>
        base_dir = Path(__file__).resolve().parents[3]

    merged: dict[str, str] = {}
    unknown_keys: list[str] = []

    for i, fname in enumerate(files):
        fpath = base_dir / fname
        required = i == 0
        if not fpath.exists():
            if required:
                raise FileNotFoundError(f"Required baseline config not found: {fpath}")
            continue
        parsed = _parse_dotenv(fpath)
        # Check for unknown keys
        for key in parsed:
            if key.upper() not in PROJECT_CONFIG_KEYS and key not in PROJECT_CONFIG_KEYS:
                unknown_keys.append(key)
        merged.update(parsed)

    if unknown_keys:
        raise ValueError(
            f"Unknown project configuration keys: {', '.join(sorted(set(unknown_keys)))}. "
            f"Only keys in PROJECT_CONFIG_KEYS are permitted in project dotenv files."
        )

    return merged


# Backend-specific keys that Settings accepts
_SETTINGS_FIELDS: frozenset[str] = frozenset()


def _get_settings_field_names() -> frozenset[str]:
    """Lazily determine the set of field names Settings accepts."""
    global _SETTINGS_FIELDS
    if not _SETTINGS_FIELDS:
        _SETTINGS_FIELDS = frozenset(Settings.model_fields.keys())
    return _SETTINGS_FIELDS


def get_settings(explicit: Mapping[str, object] | None = None) -> Settings:
    """Build and validate Settings.

    With no explicit mapping, BaseSettings reads only declared field names from the
    ambient environment — arbitrary unrelated OS variables (PATH, HOME, CI) are
    simply ignored by pydantic-settings because they don't match any field name.

    With an explicit mapping, Settings is constructed directly from it (extra=forbid
    rejects unknown keys). This is used after load_project_dotenv to pass only
    backend-relevant fields.
    """
    if explicit is not None:
        # Filter to only keys that match Settings fields (case-insensitive)
        field_names = _get_settings_field_names()
        filtered = {k.lower(): v for k, v in explicit.items() if k.lower() in field_names}
        return Settings(**filtered)
    # With no explicit mapping, pydantic-settings reads from env (only declared names).
    return Settings()
