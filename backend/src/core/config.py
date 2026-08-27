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

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
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
        # The §6.4 two-role arrangement. These are consumed by
        # `scripts/postgres-init/10-forgeops-roles.sh` inside the Postgres container,
        # not by `Settings`: the application never needs the migrator's password, and
        # giving the app process a credential it has no use for would widen the blast
        # radius of a compromise for nothing. They are registered here because
        # `.env.example` declares them and the inventory check requires every declared
        # key to be known.
        "FORGEOPS_APP_DB_PASSWORD",
        "FORGEOPS_MIGRATOR_DB_PASSWORD",
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
        # The SECOND self-hosted server, which is what makes cross-endpoint failover provable. The
        # `self_hosted` tier's secondary used to name the same base URL as its primary, so stopping
        # one server took both chain entries with it and the cascade had nowhere to fall through to.
        "SELF_HOSTED_SECONDARY_BASE_URL",
        # The self-hosted model server's identity. `SELF_HOSTED_BASE_URL` said where it is and
        # nothing said WHAT it serves, so `config/model-tiers.yaml` carried a literal model tag
        # that no real server had — every request to the only reachable endpoint answered 404.
        "SELF_HOSTED_MODEL_ID",
        "SELF_HOSTED_EMBEDDING_MODEL_ID",
        # Compose-only, like CERBOS_HTTP_PORT: the published port of the `ollama` service. The
        # application reaches the server through SELF_HOSTED_BASE_URL and never reads this.
        "OLLAMA_PORT",
        # Compose-only, as OLLAMA_PORT is: the published port of the `ollama-secondary` service.
        "OLLAMA_SECONDARY_PORT",
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
        # ─── Phase 1 additions (design §13.1) ────────────────────────────────
        # Auth (§1.11)
        "OIDC_ISSUER",
        "OIDC_APP_AUDIENCE",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URL",
        "SESSION_COOKIE_NAME",
        "SESSION_TTL_SECONDS",
        "REFRESH_TTL_SECONDS",
        "CERBOS_URL",
        # Compose-only, like the other *_PORT keys: the sidecar's published port. Task 6.4.
        "CERBOS_HTTP_PORT",
        "AUTHENTIK_SECRET_KEY",
        "AUTHENTIK_BOOTSTRAP_PASSWORD",
        "AUTHENTIK_BOOTSTRAP_TOKEN",
        # Task 6.3 added the SERVER side of Authentik to the default Compose profile, so
        # `.env.example` now carries the variables the container itself reads. They are
        # deliberately spelled with Authentik's own double underscores: Compose
        # interpolation does not read `env_file`, so a renamed variable mapped in the
        # compose `environment:` block would be empty on a fresh clone. They are listed
        # here because the allowlist is exhaustive by design — the container needs them
        # and `Settings` does not, which is exactly the case the allowlist exists to
        # permit explicitly rather than by pattern.
        "AUTHENTIK_BOOTSTRAP_EMAIL",
        "AUTHENTIK_PORT",
        "AUTHENTIK_POSTGRESQL__NAME",
        "AUTHENTIK_POSTGRESQL__USER",
        "AUTHENTIK_POSTGRESQL__PASSWORD",
        "AUTHENTIK_REDIS__DB",
        # Agent pairing and envelopes (§1.1)
        "PAIRING_CODE_TTL_SECONDS",
        "PAIRING_CODE_MAX_ATTEMPTS",
        "PAIRING_CODE_ALPHABET",
        "PAIRING_RATE_LIMIT_PER_IP_PER_MINUTE",
        "PAIRING_RATE_LIMIT_GLOBAL_PER_WINDOW",
        "DEVICE_CERT_TTL_HOURS",
        "DEVICE_CERT_RENEW_BEFORE_HOURS",
        "ENVELOPE_MAX_AGE_SECONDS",
        "ENVELOPE_CLOCK_SKEW_SECONDS",
        "ENVELOPE_PEPPER",
        "INTERNAL_CA_CERT_PEM",
        "INTERNAL_CA_KEY_PEM",
        "HEARTBEAT_INTERVAL_SECONDS",
        "HEARTBEAT_TIMEOUT_SECONDS",
        # Analysis and indexing (§1.3)
        "SCAN_MAX_FILE_SIZE_BYTES",
        "SCAN_WATCH_DEBOUNCE_MS",
        "SCAN_PARSER_CONCURRENCY",
        "CHUNK_TARGET_TOKENS",
        "CHUNK_OVERLAP_TOKENS",
        "SUMMARY_TARGET_TOKENS",
        "EMBEDDING_BACKEND",
        "EMBEDDING_MODEL_ID_LOCAL",
        "EMBEDDING_DIMS_LOCAL",
        "LLM_KEY_VOYAGE",
        "VOYAGE_BASE_URL",
        "RERANK_MODEL",
        "RETRIEVAL_OVERFETCH_FACTOR",
        "RETRIEVAL_TOP_K",
        # Generation (§1.5)
        "GENERATION_MAX_ITERATIONS",
        "JUDGE_TIER",
        "JUDGE_PROMPT_VERSION",
        "TEMPLATE_LIBRARY_PATH",
        # Which of §11.7's six tiers a generation run routes to. Registered because generation
        # had no tier at all: `generation_runs.tier` was the SQL literal `'deterministic'`, a
        # value that is not a `ModelTier`, because nothing routed.
        "GENERATION_TIER",
        # Governance, policy, audit (§1.7 §1.9 §1.10)
        "GOVERNANCE_POLICY_PACKAGE",
        "POLICY_BUNDLE_REFRESH_SECONDS",
        "APPROVAL_TTL_SECONDS",
        "AUDIT_ADVISORY_LOCK_KEY",
        # Secrets (§1.8)
        "SECRET_BACKEND",
        "LOCAL_SECRET_SEAL_KEY",
        # Tasks (§7.10)
        "TASK_DISPATCHER",
        "ARQ_QUEUE_NAME",
        "ARQ_MAX_JOBS",
        "ARQ_JOB_TIMEOUT_SECONDS",
        # Database pooling (§6.7)
        "DATABASE_POOLER_MODE",
        "ALEMBIC_DATABASE_URL",
        # Agent-side (§1.1 §1.3 §1.5)
        "AGENT_STATE_DIR",
        "AGENT_CREDENTIAL_STORE",
        "AGENT_IDENTITY_PROVIDER",
        "SPIFFE_ENDPOINT_SOCKET",
        "AGENT_JOURNAL_MAX_BYTES",
        "AGENT_JOURNAL_MAX_AGE_HOURS",
        "AGENT_JOURNAL_DRAIN_BATCH",
        "AGENT_TRIVY_BINARY",
        "AGENT_VALIDATOR_TIMEOUT_SECONDS",
        # Frontend (browser-visible; never a secret)
        "FRONTEND_BASE_URL",
        "OIDC_PUBLIC_BASE_URL",
        "NEXT_PUBLIC_OIDC_LOGIN_PATH",
        "NEXT_PUBLIC_SSE_TIMEOUT_MS",
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
    #: Where the self-hosted, OpenAI-compatible model server is.
    #:
    #: `load_tier_config` has always expanded `${SELF_HOSTED_BASE_URL}` straight from `os.environ`,
    #: so the key was declared in `PROJECT_CONFIG_KEYS` and had no field here. It needs one now
    #: because `_build_cache_embedder` reads it to decide whether L2 can be served locally, and
    #: reading it out of the process environment there would bypass the one validated view of
    #: configuration the rest of the composition root uses.
    self_hosted_base_url: str = Field(default="")

    #: The second self-hosted endpoint, expanded into `model-tiers.yaml` as the `self_hosted` tier's
    #: secondary. Declared here for the same reason `self_hosted_base_url` is: `load_tier_config`
    #: reads `os.environ` directly, so a key absent from `Settings` would be invisible to every
    #: config test while still being required by the tier file at startup.
    self_hosted_secondary_base_url: str = Field(default="")
    #: What the self-hosted model server actually serves.
    #:
    #: `SELF_HOSTED_BASE_URL` said WHERE the server is and nothing said WHAT it serves, so
    #: `config/model-tiers.yaml` carried a literal model tag (`qwen3-coder-next`) that no real
    #: server has. Every request to the one endpoint a fresh clone can reach therefore came back
    #: 404 `model "..." not found`. Empty means "not configured", which leaves the tier YAML's
    #: `${SELF_HOSTED_MODEL_ID}` unexpandable and is a startup error — deliberately louder than a
    #: default, because a default reintroduces the same 404 silently.
    self_hosted_model_id: str = Field(default="")
    #: The embedding model behind the L2 semantic cache, on the same server.
    #:
    #: Empty leaves the cache L1-only, which is correct and safe rather than degraded — see
    #: `main.py::_build_cache_embedder` for why an input-INSENSITIVE embedder is worse than none.
    self_hosted_embedding_model_id: str = Field(default="")
    outbound_http_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    ai_rate_limit_capacity: int = Field(default=20, ge=1)
    ai_rate_limit_refill_per_second: float = Field(default=0.2, gt=0)
    ai_rate_limit_fail_mode: Literal["fail_closed"] = "fail_closed"

    # CORS
    cors_allow_origins: str = Field(default="http://localhost:3000")

    # ─── Phase 1 §1.11 auth ──────────────────────────────────────────────────
    # The app API audience is DISTINCT from the MCP gateway audience, so a token
    # minted for the gateway cannot be replayed against the product API (§7.1).
    #
    # Deviation from the §7.1 snippet, recorded deliberately: the snippet declares
    # these with no default, which would make Settings unconstructable without a full
    # environment and break every Phase 0 unit test that builds it from two DSNs. The
    # file already has the right pattern for this — `mcp_oidc_issuers` defaults to
    # empty and a validator refuses an empty value when APP_ENV=production — so the
    # same shape is used here. Dev and test boot; production still fails fast, and it
    # fails fast on ALL of them together rather than on the first (P-15).
    #
    # `internal_ca_cert_pem` in particular MUST tolerate empty: §13.1 ships it blank
    # because scripts/init-ca.sh populates it locally and key material is never
    # committed.
    oidc_issuer: str = Field(default="")
    oidc_app_audience: str = Field(default="forgeops-api")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: SecretStr = Field(default=SecretStr(""))
    oidc_redirect_url: str = Field(default="http://localhost:8000/api/v1/auth/callback")
    #: Where a BROWSER reaches the IdP, when that is not where the backend reaches it.
    #:
    #: Empty means they are the same, which is correct for a single host and is the default. Set it
    #: when the backend talks to the IdP over an internal name a browser cannot resolve -- inside
    #: Compose the backend uses `authentik-server:9000` while the browser must use the published
    #: `localhost:<port>`, and neither address works from the other side. Only the authorization
    #: redirect is rewritten; discovery, token and JWKS stay on `oidc_issuer`, which is also what
    #: keeps the token's `iss` claim equal to it.
    oidc_public_base_url: str = Field(default="")
    #: Where a BROWSER is sent once the code exchange succeeds.
    #:
    #: Distinct from `oidc_redirect_url`, and the distinction is the point: the IdP redirects to
    #: the BACKEND callback, because that is where the client secret and the pending PKCE verifier
    #: live. But the backend callback answers JSON, so a browser that followed the IdP's redirect
    #: would be left staring at a token document instead of the application. This is the origin the
    #: callback bounces it to afterwards, with `next` appended.
    #:
    #: Only ever joined with a path `_safe_next` has already reduced to a same-origin absolute
    #: path, so a hostile `next` cannot turn this into an open redirect.
    frontend_base_url: str = Field(default="http://localhost:3000")
    session_cookie_name: str = Field(default="forgeops_session")
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    refresh_ttl_seconds: int = Field(default=1_209_600, ge=3600)
    cerbos_url: AnyHttpUrl = Field(default="http://cerbos:3592")  # type: ignore[assignment]
    authentik_secret_key: SecretStr = Field(default=SecretStr(""))
    authentik_bootstrap_password: SecretStr = Field(default=SecretStr(""))
    authentik_bootstrap_token: SecretStr = Field(default=SecretStr(""))

    # ─── Phase 1 §1.1 pairing and envelope integrity ─────────────────────────
    pairing_code_ttl_seconds: int = Field(default=300, ge=60, le=900)
    pairing_code_max_attempts: int = Field(default=5, ge=1, le=10)
    # Crockford base32: no I, L, O or U, so a spoken or retyped code cannot be
    # ambiguous. Validated below rather than trusted.
    pairing_code_alphabet: str = Field(default="0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    pairing_rate_limit_per_ip_per_minute: int = Field(default=10, ge=1, le=120)
    # §14.6's second bound, and the one that binds a DISTRIBUTED attacker. The per-IP
    # cap says nothing about many IPs; §14.6 computes P ≈ 5.6 × 10⁻⁶ from "total
    # attempts across the window cannot exceed 600", and this is that 600. The window
    # is PAIRING_CODE_TTL_SECONDS — a code's own lifetime — so the arithmetic stays
    # true when the TTL is changed rather than silently referring to five minutes that
    # are no longer five minutes.
    pairing_rate_limit_global_per_window: int = Field(default=600, ge=1, le=100_000)
    device_cert_ttl_hours: int = Field(default=24, ge=1, le=168)
    device_cert_renew_before_hours: int = Field(default=6, ge=1, le=168)
    envelope_max_age_seconds: int = Field(default=300, ge=30, le=900)
    envelope_clock_skew_seconds: int = Field(default=60, ge=0, le=300)
    envelope_pepper: SecretStr = Field(default=SecretStr(""))
    internal_ca_cert_pem: SecretStr = Field(default=SecretStr(""))
    internal_ca_key_pem: SecretStr = Field(default=SecretStr(""))
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    heartbeat_timeout_seconds: int = Field(default=90, ge=10, le=900)

    # ─── Phase 1 §1.3 analysis and indexing ──────────────────────────────────
    scan_max_file_size_bytes: int = Field(default=1_048_576, ge=1024)
    scan_watch_debounce_ms: int = Field(default=250, ge=0, le=10_000)
    # 0 means min(GOMAXPROCS, 8); the agent owns that resolution.
    scan_parser_concurrency: int = Field(default=0, ge=0, le=64)
    chunk_target_tokens: int = Field(default=512, ge=128, le=2048)
    chunk_overlap_tokens: int = Field(default=128, ge=0, le=512)
    summary_target_tokens: int = Field(default=1024, ge=256, le=4096)
    # Selects WHICH vector table is written; the two must never be mixed (D-48).
    embedding_backend: Literal["voyage", "bge_m3"] = "voyage"
    embedding_model_id_local: str = Field(default="bge-m3")
    embedding_dims_local: int = Field(default=1024, ge=1)
    llm_key_voyage: SecretStr = Field(default=SecretStr(""))
    voyage_base_url: str = Field(default="https://api.voyageai.com/v1")
    rerank_model: str = Field(default="voyage-rerank-2")
    retrieval_overfetch_factor: int = Field(default=3, ge=1, le=10)
    retrieval_top_k: int = Field(default=12, ge=1, le=200)

    # ─── Phase 1 §1.5 generation bounds ──────────────────────────────────────
    # NOT an int. NFR-04 targets "under 3 iterations average" and phases.md §1.5 fixes
    # the maximum at 3. Typed as int, an operator could set 10 and quietly move a
    # safety-relevant bound that Q-08 exists to guarantee; typed as Literal[3] the
    # configuration refuses to load. A different bound is a decision, not a variable.
    generation_max_iterations: Literal[3] = 3
    judge_tier: str = Field(default="medium_value")
    judge_prompt_version: int = Field(default=1, ge=1)
    template_library_path: str = Field(default="src/generation/templates")
    #: Which tier a generation run routes to (§11.5.4).
    #:
    #: A `Literal` rather than a free string, because an unknown tier makes
    #: `ModelRouter.complete` return EXHAUSTED for every request — the run falls back to a
    #: template and looks like a provider outage instead of a typo.
    #:
    #: The six names are RESTATED here rather than read from `ai.routing.tiers.ModelTier`, which is
    #: not the repository's usual preference. `TID251` bans a cross-domain import from `src.core`,
    #: and correctly: configuration is the lowest layer and every module that reads settings would
    #: pay for a cycle through `src.ai`. The drift that restating invites is closed by
    #: `tests/unit/test_generation_tier_setting.py`, which asserts this tuple equals `ModelTier`
    #: exactly — so a tier added to the enum fails a test rather than becoming quietly
    #: unconfigurable.
    generation_tier: Literal[
        "high_coding",
        "high_analysis",
        "medium",
        "medium_value",
        "low_logs",
        "self_hosted",
    ] = "self_hosted"

    # ─── Phase 1 §1.7 §1.9 §1.10 governance, policy, audit ───────────────────
    governance_policy_package: str = Field(default="forgeops/governance")
    policy_bundle_refresh_seconds: int = Field(default=300, ge=30)
    approval_ttl_seconds: int = Field(default=604_800, ge=60)
    audit_advisory_lock_key: str = Field(default="forgeops-audit")

    # ─── Phase 1 §1.8 secrets ────────────────────────────────────────────────
    secret_backend: Literal["infisical", "local"] = "infisical"
    local_secret_seal_key: SecretStr = Field(default=SecretStr(""))
    infisical_url: AnyHttpUrl = Field(default="http://infisical:8080")  # type: ignore[assignment]
    infisical_client_id: str = Field(default="")
    infisical_client_secret: SecretStr = Field(default=SecretStr(""))
    infisical_project_id: str = Field(default="")

    # ─── Phase 1 §7.10 tasks ─────────────────────────────────────────────────
    task_dispatcher: Literal["arq", "inline"] = "arq"
    arq_queue_name: str = Field(default="forgeops")
    arq_max_jobs: int = Field(default=10, ge=1, le=1000)
    arq_job_timeout_seconds: int = Field(default=900, ge=1)

    # ─── Phase 1 §6.7 database pooling ───────────────────────────────────────
    # `transaction` mode means a pooler hands out a different backend connection per
    # transaction, so asyncpg's prepared-statement cache must be disabled (§7.12).
    database_pooler_mode: Literal["session", "transaction"] = "session"
    alembic_database_url: str = Field(default="")

    # ─── Phase 1 agent-side keys ─────────────────────────────────────────────
    # Present in the shared .env.example namespace so the Go agent and the backend
    # validate one inventory; the backend does not consume most of them.
    agent_state_dir: str = Field(default="")
    agent_credential_store: Literal["auto", "keychain", "file"] = "auto"
    agent_identity_provider: Literal["paired_device", "spiffe_workload"] = "paired_device"
    spiffe_endpoint_socket: str = Field(default="")
    agent_journal_max_bytes: int = Field(default=67_108_864, ge=0)
    agent_journal_max_age_hours: int = Field(default=168, ge=1)
    agent_journal_drain_batch: int = Field(default=64, ge=1, le=10_000)
    agent_trivy_binary: str = Field(default="trivy")
    agent_validator_timeout_seconds: int = Field(default=120, ge=1)

    @field_validator("mcp_oidc_issuers")
    @classmethod
    def _require_issuer_in_production(cls, v: str, info: Any) -> str:
        """An empty issuer allowlist in production would accept nothing — refuse to boot."""
        if not v.strip() and info.data.get("app_env") == "production":
            raise ValueError("MCP_OIDC_ISSUERS must be non-empty when APP_ENV=production")
        return v

    @field_validator("generation_max_iterations", mode="before")
    @classmethod
    def _coerce_iteration_bound(cls, v: Any) -> Any:
        """Accept the string form an env var always arrives as, then let Literal judge.

        `Literal[3]` does not coerce `"3"`, and every value from a dotenv or the
        process environment is a string — so without this the committed baseline
        cannot load at all. The coercion is deliberately narrow: only a decimal
        integer literal is converted, and the conversion does not widen what is
        ACCEPTED. `GENERATION_MAX_ITERATIONS=10` still becomes `10` here and is then
        refused by `Literal[3]`, which is the guarantee Q-08 depends on.
        """
        if isinstance(v, str) and v.strip().lstrip("+").isdigit():
            return int(v.strip())
        return v

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def _overlap_below_target(cls, v: int, info: Any) -> int:
        """Overlap >= target is not a tuning choice, it is a non-terminating chunker.

        With overlap >= target every window re-emits its predecessor's whole content,
        so chunk count grows without bound on a large file. Caught here rather than
        as a mysterious memory profile in §10.8.3.
        """
        target = info.data.get("chunk_target_tokens", 512)
        if v >= target:
            raise ValueError(f"CHUNK_OVERLAP_TOKENS ({v}) must be smaller than CHUNK_TARGET_TOKENS ({target})")
        return v

    @field_validator("pairing_code_alphabet")
    @classmethod
    def _alphabet_is_unambiguous(cls, v: str) -> str:
        """Crockford base32 excludes I, L, O and U for a reason.

        A pairing code is read aloud or retyped from a terminal. `I`/`1`, `O`/`0` and
        `U`/`V` confusions turn into an indistinguishable `pairing-code-invalid`
        (§11.2), which is unhelpful precisely because the response is deliberately
        opaque. Duplicates are also rejected: they silently skew the entropy the code
        is assumed to carry.
        """
        if len(set(v)) != len(v):
            raise ValueError("PAIRING_CODE_ALPHABET contains duplicate characters")
        forbidden = set("ILOU") & set(v.upper())
        if forbidden:
            raise ValueError(
                f"PAIRING_CODE_ALPHABET must exclude the ambiguous characters {sorted(forbidden)} (Crockford base32)"
            )
        if len(v) < 16:
            raise ValueError("PAIRING_CODE_ALPHABET needs at least 16 symbols to carry enough entropy")
        return v

    @field_validator("device_cert_renew_before_hours")
    @classmethod
    def _renew_before_expiry(cls, v: int, info: Any) -> int:
        """Renewing after expiry is not renewal; the session would already be dead."""
        ttl = info.data.get("device_cert_ttl_hours", 24)
        if v >= ttl:
            raise ValueError(f"DEVICE_CERT_RENEW_BEFORE_HOURS ({v}) must be smaller than DEVICE_CERT_TTL_HOURS ({ttl})")
        return v

    @field_validator("heartbeat_timeout_seconds")
    @classmethod
    def _timeout_exceeds_interval(cls, v: int, info: Any) -> int:
        """A timeout at or below the interval declares every healthy agent dead."""
        interval = info.data.get("heartbeat_interval_seconds", 30)
        if v <= interval:
            raise ValueError(f"HEARTBEAT_TIMEOUT_SECONDS ({v}) must exceed HEARTBEAT_INTERVAL_SECONDS ({interval})")
        return v

    @model_validator(mode="before")
    @classmethod
    def _reject_blast_radius_override_in_production(cls, values: Any) -> Any:
        """D-39: `MCP_AGENT_BLAST_RADIUS` is a development default only.

        Phase 0 read the agent's blast radius from configuration because no agent
        existed yet (OQ-20). Phase 1 derives it from the attested agent identity, so
        the variable is demoted: it supplies a default when no identity is present,
        and its PRESENCE in production is a startup error rather than a silently
        honoured widening of authority.

        Checked in `mode="before"` because that is the only place presence is
        distinguishable from the default — after validation, an unset variable and an
        explicit `read_only` are the same value, and refusing the value rather than
        its presence would mean an operator could still widen authority to
        `infrastructure` by setting it in a non-production environment name.
        """
        if not isinstance(values, dict):
            return values

        lowered = {str(k).lower(): v for k, v in values.items()}
        if str(lowered.get("app_env", "")).strip().lower() != "production":
            return values
        if "mcp_agent_blast_radius" in lowered:
            raise ValueError(
                "MCP_AGENT_BLAST_RADIUS must not be set when APP_ENV=production: in "
                "production the agent's blast radius is derived from its attested "
                "identity, and this variable is a development default only (D-39). "
                "Remove it from the environment."
            )
        return values

    @model_validator(mode="after")
    def _require_production_secrets(self) -> Settings:
        """Every credential a deployment needs, reported together (P-15).

        One error per missing value would make bringing up an environment a sequence of restarts.
        The Phase 0 contract is that configuration problems are accumulated into ONE report, so this
        collects them all before raising.

        ENVELOPE_PEPPER IS REQUIRED IN EVERY ENVIRONMENT, NOT ONLY PRODUCTION

        Everything else in this list is a credential a developer legitimately does not need to run
        tests — nobody should require an OIDC client secret to exercise the tier router. The pepper
        is different in kind. An empty one is not a MISSING credential, it is a SILENTLY BROKEN one:

        * `HMAC-SHA256` under an empty key still computes. Device tokens and pairing codes would be
          stored under an unkeyed digest, so anyone able to read `agent_devices` could forge the
          stored HMAC for any device by hashing a value of their own choosing.
        * `derive_key_encryption_key` HKDFs the pepper (D-62), so an empty pepper derives the SAME
          key-encryption key in every deployment on earth. `envelope_key_enc` from one installation
          would unseal in another, while the column name still asserted ciphertext.

        Neither fails loudly at the point of use, which is the argument for checking at the boundary.

        WHAT THIS REPLACES. `DeviceService`, `derive_key_encryption_key`, `auth/sessions.py` and
        `GovernanceChokepoint` each raise on an empty pepper already — four separate voices, each
        reached only when something first tries to use it. So the process started, answered
        `/health/live`, and then died on the first pairing attempt with a message about whichever
        component got there first. `credentials.md` said "the backend refuses to start without it",
        which was the intent and not the behaviour; this makes the document true rather than
        weakening it to match the code. The downstream guards stay: they are constructed directly in
        tests and could be handed a pepper that never came through `Settings`.

        Checked INSIDE this validator rather than in one of its own, so P-15's single report survives
        — a separate validator raising first would have hidden every other missing production
        credential behind the pepper, turning one report back into a sequence of restarts.
        """
        if self.app_env != "production":
            return self

        required: list[tuple[str, str]] = [
            ("OIDC_ISSUER", self.oidc_issuer),
            ("OIDC_CLIENT_ID", self.oidc_client_id),
            ("OIDC_CLIENT_SECRET", self.oidc_client_secret.get_secret_value()),
            ("ENVELOPE_PEPPER", self.envelope_pepper.get_secret_value()),
            ("INTERNAL_CA_CERT_PEM", self.internal_ca_cert_pem.get_secret_value()),
            ("INTERNAL_CA_KEY_PEM", self.internal_ca_key_pem.get_secret_value()),
        ]
        if self.secret_backend == "local":
            required.append(("LOCAL_SECRET_SEAL_KEY", self.local_secret_seal_key.get_secret_value()))

        missing = [name for name, value in required if not str(value).strip()]
        if missing:
            raise ValueError("the following must be non-empty when APP_ENV=production: " + ", ".join(sorted(missing)))
        return self

    @model_validator(mode="after")
    def _require_envelope_pepper_everywhere(self) -> Settings:
        """`ENVELOPE_PEPPER` must also be non-empty OUTSIDE production.

        WHY IT IS NOT ENOUGH TO LEAVE IT IN THE LIST ABOVE

        That validator returns early unless `APP_ENV=production`, which is right for the credentials
        beside it — nobody should need an OIDC client secret to exercise the tier router. The pepper
        is different in kind. An empty one is not a MISSING credential, it is a SILENTLY BROKEN one:

        * `HMAC-SHA256` under an empty key still computes. Device tokens and pairing codes would be
          stored under an unkeyed digest, so anyone able to read `agent_devices` could forge the
          stored HMAC for any device by hashing a value of their own choosing.
        * `derive_key_encryption_key` HKDFs the pepper (D-62), so an empty pepper derives the SAME
          key-encryption key in every deployment on earth. `envelope_key_enc` from one installation
          would unseal in another, while the column name still asserted ciphertext.

        Neither fails loudly at the point of use, which is the argument for the boundary.

        WHAT THIS REPLACES. `DeviceService`, `derive_key_encryption_key`, `auth/sessions.py` and
        `GovernanceChokepoint` each raise on an empty pepper already — four separate voices, each
        reached only when something first tries to use it. So the process started, answered
        `/health/live`, and then died on the first pairing attempt with a message about whichever
        component got there first. `credentials.md` said "the backend refuses to start without it",
        which was the intent and not the behaviour; this makes the document true rather than
        weakening it to match the code. The downstream guards stay: they are constructed directly in
        tests and could be handed a pepper that never came through `Settings`.

        DECLARED AFTER the production validator ON PURPOSE. Pydantic runs `mode="after"` validators
        in definition order, so in production the accumulated report still raises first and still
        names the pepper among everything else — P-15's one-report contract is untouched. Declaring
        this first would have hidden every other missing production credential behind the pepper,
        turning one report back into a sequence of restarts, which a test caught by name.
        """
        if not self.envelope_pepper.get_secret_value().strip():
            raise ValueError(
                "ENVELOPE_PEPPER must be non-empty in every environment. An empty pepper is not a "
                "missing credential but a broken one: HMAC-SHA256 under an empty key still computes, "
                "so every device token and pairing code would be stored unkeyed, and the "
                "HKDF-derived key-encryption key (D-62) would be identical in every deployment. "
                "Set it in .env; .env.example ships a development value."
            )
        return self

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
