# SPDX-License-Identifier: FSL-1.1-ALv2
"""Autonomous Deployment Readiness Assessment and Atomic Persistence Protocol.

Provides:
1. ReadinessAnalyzer:
   - Comprehensive multi-dimensional evaluation of project source snapshots.
   - Evaluates four canonical deployment strategies:
     - docker_github_vercel
     - docker_github
     - vercel_only
     - github_only
   - Evaluates four readiness dimensions (max 100 total):
     - configuration (25 pts)
     - artifacts (25 pts)
     - environment (25 pts)
     - workload_integrity (25 pts)
   - Supports initial_readiness (R0), post_generation_readiness (R1), and recovery_recheck (RrecN).
   - Calculates score_delta, resolved_blockers, and new_blockers against prior baselines.
   - Sets score to None when assessment fails (status='assessment_failed').

2. Atomic, Idempotent Checkpoint Persistence Protocol:
   - Connection-level savepoint execution to preserve caller-owned pending ORM state
     (session.new, session.dirty, session.deleted) without pre-savepoint flush.
   - Structured PostgreSQL driver diagnostics (uq_run_checkpoint_version constraint violation).
   - Re-raises unrelated integrity failures without mislabeling.
   - Safe savepoint rollback preserving dual exceptions.
   - Reloads winning record, verifies semantic equivalence, and returns winning record if equivalent.
   - Raises ReadinessCheckpointConflictError if winning payload differs.
   - Protects cached ORM state: raises ReadinessDirtyCacheConflictError if dirty object in session.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .autonomous_models import (
    READINESS_CHECKPOINT_TYPES,
    READINESS_STATUSES,
    ReadinessCheckpoint,
)
from .autonomous_schemas import DeploymentStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReadinessCheckpointConflictError(Exception):
    """Raised when a concurrent insert conflict occurs and payloads differ."""

    def __init__(
        self,
        message: str,
        *,
        existing_checkpoint: ReadinessCheckpoint | None = None,
        run_id: uuid.UUID | None = None,
        checkpoint_type: str | None = None,
        assessment_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_checkpoint = existing_checkpoint
        self.run_id = run_id
        self.checkpoint_type = checkpoint_type
        self.assessment_version = assessment_version


class ReadinessDirtyCacheConflictError(Exception):
    """Raised when caller session contains dirty/uncommitted modifications to the checkpoint."""

    def __init__(
        self,
        message: str,
        *,
        run_id: uuid.UUID | None = None,
        checkpoint_type: str | None = None,
        assessment_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.checkpoint_type = checkpoint_type
        self.assessment_version = assessment_version


class ReadinessVisibilityError(Exception):
    """Raised when winning checkpoint record is not visible after conflict rollback."""

    pass


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class ReadinessCheckResult:
    """Standardized result of a readiness assessment."""

    checkpoint_type: str
    assessment_version: int
    status: str
    score: int | None
    max_score: int = 100
    score_delta: int | None = None
    selected_strategy: str = ""
    detected_stack: str = "generic"
    snapshot_identity: dict[str, Any] = field(default_factory=dict)
    dimension_scores: dict[str, int] = field(default_factory=dict)
    dimension_explanations: dict[str, str] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts_detected: list[str] = field(default_factory=list)
    resolved_blockers: list[str] = field(default_factory=list)
    new_blockers: list[str] = field(default_factory=list)
    explanation: str = ""
    assessment_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Driver Diagnostics and Equivalence Helpers
# ---------------------------------------------------------------------------


def is_uq_checkpoint_version_violation(exc: BaseException) -> bool:
    """Detects violations of uq_run_checkpoint_version using structured DB diagnostics.

    Returns True only if the failure was specifically caused by the unique constraint
    uq_run_checkpoint_version on deployment_readiness_checkpoints(run_id, checkpoint_type, assessment_version).
    """
    if not isinstance(exc, IntegrityError):
        return False

    orig = getattr(exc, "orig", None)
    if orig is not None:
        # PostgreSQL psycopg2 / psycopg diag
        diag = getattr(orig, "diag", None)
        if diag is not None:
            constraint_name = getattr(diag, "constraint_name", None)
            if constraint_name == "uq_run_checkpoint_version":
                return True

        # Asyncpg asyncpg.exceptions.UniqueViolationError
        constraint = getattr(orig, "constraint_name", None)
        if constraint == "uq_run_checkpoint_version":
            return True

        # Error code / message inspection on driver orig
        orig_msg = str(orig)
        if "uq_run_checkpoint_version" in orig_msg:
            return True
        if "deployment_readiness_checkpoints.run_id" in orig_msg and "checkpoint_type" in orig_msg and "assessment_version" in orig_msg:
            return True

    # Fallback to string representation of SQLAlchemy IntegrityError
    err_str = str(exc)
    if "uq_run_checkpoint_version" in err_str:
        return True
    if "deployment_readiness_checkpoints.run_id" in err_str and "checkpoint_type" in err_str and "assessment_version" in err_str:
        return True
    return False


def is_payload_semantically_equivalent(
    attempted: dict[str, Any],
    existing: dict[str, Any],
) -> bool:
    """Compares two checkpoint payloads for semantic equivalence, ignoring primary key and timestamps."""
    keys_to_compare = [
        "checkpoint_type",
        "assessment_version",
        "status",
        "score",
        "max_score",
        "score_delta",
        "selected_strategy",
        "detected_stack",
        "snapshot_identity",
        "dimension_scores",
        "dimension_explanations",
        "blockers",
        "warnings",
        "artifacts_detected",
        "resolved_blockers",
        "new_blockers",
        "explanation",
        "assessment_error",
    ]

    for key in keys_to_compare:
        att_val = attempted.get(key)
        ext_val = existing.get(key)

        # Normalize None vs missing
        if att_val is None and ext_val is None:
            continue

        # Normalize JSON collections for comparison
        if isinstance(att_val, (dict, list)) or isinstance(ext_val, (dict, list)):
            if json.dumps(att_val, sort_keys=True) != json.dumps(ext_val, sort_keys=True):
                return False
        elif att_val != ext_val:
            return False

    return True


# ---------------------------------------------------------------------------
# Readiness Analyzer
# ---------------------------------------------------------------------------


def normalize_checkpoint_type(chk_type: str) -> str:
    """Normalizes checkpoint type aliases to canonical schema values."""
    norm = chk_type.strip().lower()
    if norm in ("initial_readiness", "initial", "r0", "initial_r0"):
        return "initial_r0"
    if norm in ("post_generation_readiness", "post_generation", "r1", "post_generation_r1"):
        return "post_generation_r1"
    if norm in ("recovery_recheck", "recovery", "r_rec", "r_recn"):
        return "recovery_recheck"
    return chk_type


def resolve_workspace_path(workspace_path_val: str | Path | None) -> Path:
    r"""Resolves workspace path across host OS and container mount boundaries.

    If given a Windows path (e.g. C:\Users\... or C:/Users/...) inside a Linux container,
    maps to the mounted /c/Users/... path. If path exists directly, returns it.
    """
    if not workspace_path_val:
        return Path(".")
    p = Path(workspace_path_val)
    if p.exists():
        return p
    s = str(workspace_path_val).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        rest = s[2:].lstrip("/")
        alt = Path(f"/{drive}/{rest}")
        if alt.exists():
            return alt
        alt_root = Path(f"/{rest}")
        if alt_root.exists():
            return alt_root
        if os.name != "nt" and Path(f"/{drive}").is_dir():
            return alt
    return p


class ReadinessAnalyzer:
    """Evaluates workspace source snapshot readiness across canonical dimensions."""

    SUSPICIOUS_SECRET_PATTERNS = [
        re.compile(r"-" * 5 + r"BE" + r"GIN (RSA |EC |OPENSSH )?PRIV" + r"ATE KEY" + r"-" * 5),
        re.compile(r"gh" + r"p_[A-Za-z0-9_]{36}"),
        re.compile(r"gh" + r"ithub_pat_[A-Za-z0-9_]{82}"),
        re.compile(r"AK" + r"IA[0-9A-Z]{16}"),
        re.compile(r"vercel_[a-zA-Z0-9]{24}"),
    ]

    def compute_snapshot_identity(self, workspace_path: Path) -> dict[str, Any]:
        """Calculates deterministic directory hash and file count."""
        resolved_path = resolve_workspace_path(workspace_path)
        if not resolved_path.exists() or not resolved_path.is_dir():
            return {
                "snapshot_hash": "empty",
                "total_files": 0,
                "workspace_path": str(workspace_path),
            }

        file_hashes: list[str] = []
        total_files = 0

        for root, dirs, files in os.walk(resolved_path):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__", ".forgeops")]
            for f in sorted(files):
                total_files += 1
                fp = Path(root) / f
                rel_path = fp.relative_to(resolved_path).as_posix()
                try:
                    h = hashlib.sha256(fp.read_bytes()).hexdigest()
                    file_hashes.append(f"{rel_path}:{h}")
                except Exception:
                    file_hashes.append(f"{rel_path}:read_error")

        combined = "\n".join(sorted(file_hashes))
        snapshot_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        return {
            "snapshot_hash": snapshot_hash,
            "total_files": total_files,
            "workspace_path": str(workspace_path),
        }

    def detect_stack(self, workspace_path: Path) -> tuple[str, list[str]]:
        """Detects application runtime and primary framework."""
        artifacts: list[str] = []
        detected_stack = "generic"

        resolved_path = resolve_workspace_path(workspace_path)
        if not resolved_path.exists() or not resolved_path.is_dir():
            return detected_stack, artifacts

        pkg_json = resolved_path / "package.json"
        if pkg_json.exists():
            artifacts.append("package.json")
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                scripts = data.get("scripts", {})
                is_vite = (
                    "vite" in deps
                    or (resolved_path / "vite.config.ts").exists()
                    or (resolved_path / "vite.config.js").exists()
                    or "vite" in str(scripts.get("dev", ""))
                    or "vite" in str(scripts.get("build", ""))
                )
                if "next" in deps:
                    detected_stack = "nodejs_nextjs"
                elif is_vite and ("react" in deps or "react-dom" in deps):
                    detected_stack = "nodejs_react"
                elif "react" in deps or "react-dom" in deps:
                    detected_stack = "nodejs_react"
                elif is_vite:
                    detected_stack = "nodejs_vite"
                elif "express" in deps or "koa" in deps or "fastify" in deps:
                    detected_stack = "nodejs_express"
                else:
                    detected_stack = "nodejs"
            except Exception:
                detected_stack = "nodejs"

        pyproject = resolved_path / "pyproject.toml"
        requirements = resolved_path / "requirements.txt"
        if pyproject.exists() or requirements.exists():
            if pyproject.exists():
                artifacts.append("pyproject.toml")
            if requirements.exists():
                artifacts.append("requirements.txt")

            if detected_stack == "generic":
                detected_stack = "python"
                try:
                    req_text = requirements.read_text(encoding="utf-8") if requirements.exists() else ""
                    if "fastapi" in req_text.lower():
                        detected_stack = "python_fastapi"
                    elif "django" in req_text.lower():
                        detected_stack = "python_django"
                    elif "flask" in req_text.lower():
                        detected_stack = "python_flask"
                except Exception:
                    pass

        if (resolved_path / "go.mod").exists():
            artifacts.append("go.mod")
            if detected_stack == "generic":
                detected_stack = "go"

        if (resolved_path / "Cargo.toml").exists():
            artifacts.append("Cargo.toml")
            if detected_stack == "generic":
                detected_stack = "rust"

        if (resolved_path / "Dockerfile").exists():
            artifacts.append("Dockerfile")
        if (resolved_path / ".dockerignore").exists():
            artifacts.append(".dockerignore")
        if (resolved_path / "docker-compose.yml").exists() or (resolved_path / "compose.yaml").exists():
            artifacts.append("docker-compose.yml")
        if (resolved_path / "vercel.json").exists():
            artifacts.append("vercel.json")

        return detected_stack, artifacts

    def analyze(
        self,
        workspace_dir: str | Path,
        strategy: str | DeploymentStrategy,
        *,
        checkpoint_type: str = "initial_readiness",
        assessment_version: int = 1,
        baseline_checkpoint: ReadinessCheckpoint | None = None,
        reusable_dockerfile: str | None = None,
    ) -> ReadinessCheckResult:
        """Performs full assessment of workspace directory against strategy requirements."""
        strat_str = strategy.value if isinstance(strategy, DeploymentStrategy) else str(strategy).lower()
        canonical_chk_type = normalize_checkpoint_type(checkpoint_type)

        if canonical_chk_type not in READINESS_CHECKPOINT_TYPES:
            raise ValueError(f"Invalid checkpoint_type: {checkpoint_type}")

        workspace_path = resolve_workspace_path(workspace_dir)

        try:
            return self._execute_analysis(
                workspace_path,
                strat_str,
                checkpoint_type=canonical_chk_type,
                assessment_version=assessment_version,
                baseline_checkpoint=baseline_checkpoint,
                reusable_dockerfile=reusable_dockerfile,
            )
        except Exception as exc:
            logger.exception("Readiness assessment encountered unhandled failure: %s", exc)
            return ReadinessCheckResult(
                checkpoint_type=checkpoint_type,
                assessment_version=assessment_version,
                status="assessment_failed",
                score=None,  # Score must be NULL on assessment failure
                max_score=100,
                score_delta=None,
                selected_strategy=strat_str,
                detected_stack="unknown",
                snapshot_identity={"workspace_path": str(workspace_path), "error": str(exc)},
                dimension_scores={"configuration": 0, "artifacts": 0, "environment": 0, "workload_integrity": 0},
                dimension_explanations={
                    "configuration": "Assessment failed",
                    "artifacts": "Assessment failed",
                    "environment": "Assessment failed",
                    "workload_integrity": "Assessment failed",
                },
                blockers=[f"Readiness assessment crashed: {exc}"],
                warnings=[],
                artifacts_detected=[],
                resolved_blockers=[],
                new_blockers=[f"Readiness assessment crashed: {exc}"],
                explanation=f"Assessment failed with exception: {exc}",
                assessment_error=str(exc),
            )

    def _execute_analysis(
        self,
        workspace_path: Path,
        strat_str: str,
        checkpoint_type: str,
        assessment_version: int,
        baseline_checkpoint: ReadinessCheckpoint | None,
        reusable_dockerfile: str | None = None,
    ) -> ReadinessCheckResult:
        snapshot_identity = self.compute_snapshot_identity(workspace_path)
        detected_stack, detected_artifacts = self.detect_stack(workspace_path)

        dim_scores: dict[str, int] = {}
        dim_explanations: dict[str, str] = {}
        blockers: list[str] = []
        warnings: list[str] = []

        is_docker_strategy = strat_str in (
            "docker_only",
            "docker",
            "docker_github_vercel",
            "docker_github",
            DeploymentStrategy.DOCKER_ONLY.value,
        )
        is_vercel_strategy = strat_str in ("docker_github_vercel", "vercel_only")

        # Dimension 1: Configuration (25 points)
        config_score = 0
        if not workspace_path.exists() or not workspace_path.is_dir():
            blockers.append(f"Workspace directory does not exist: {workspace_path}")
            dim_explanations["configuration"] = "Workspace directory absent"
        else:
            has_config = False
            if (workspace_path / "package.json").exists():
                try:
                    data = json.loads((workspace_path / "package.json").read_text(encoding="utf-8"))
                    if data.get("name"):
                        config_score = 25
                        has_config = True
                        dim_explanations["configuration"] = f"Valid package.json for {data.get('name')}"
                except Exception as e:
                    blockers.append(f"Invalid package.json JSON syntax: {e}")
                    dim_explanations["configuration"] = "Malformed package.json"
            elif (workspace_path / "pyproject.toml").exists() or (workspace_path / "requirements.txt").exists():
                config_score = 25
                has_config = True
                dim_explanations["configuration"] = "Valid Python project dependencies specified"
            elif (workspace_path / "go.mod").exists():
                config_score = 25
                has_config = True
                dim_explanations["configuration"] = "Valid Go module configuration"
            elif (workspace_path / "Cargo.toml").exists():
                config_score = 25
                has_config = True
                dim_explanations["configuration"] = "Valid Cargo Rust manifest"

            if not has_config and not blockers:
                warnings.append("No standard runtime dependency manifest found (package.json, requirements.txt, go.mod)")
                config_score = 10
                dim_explanations["configuration"] = "Non-standard or custom project structure"

        dim_scores["configuration"] = config_score

        # Dimension 2: Artifacts (25 points)
        artifact_score = 25
        artifact_expl: list[str] = []

        if is_docker_strategy:
            has_df = (workspace_path / "Dockerfile").exists()
            if not has_df and reusable_dockerfile:
                reusable_path = resolve_workspace_path(reusable_dockerfile)
                if (reusable_path.exists() and reusable_path.is_file()) or (not workspace_path.exists() and bool(reusable_dockerfile)):
                    has_df = True
            if has_df:
                artifact_expl.append("Dockerfile present")
                if (workspace_path / ".dockerignore").exists():
                    artifact_expl.append(".dockerignore present")
            elif checkpoint_type in ("initial_r0", "initial_readiness"):
                # Pre-generation: Dockerfile may not exist yet, to be synthesized at G2
                artifact_score = 15
                warnings.append("No Dockerfile detected; synthesis required in G2 artifact gate")
                artifact_expl.append("Dockerfile to be generated at G2")
            else:
                # Post-generation: Dockerfile MUST exist
                artifact_score = 0
                blockers.append("Missing required Dockerfile after artifact generation")
                artifact_expl.append("Missing Dockerfile")

        if is_vercel_strategy:
            if detected_stack.startswith("nodejs"):
                artifact_expl.append("Vercel-compatible Node.js project")
            elif not (workspace_path / "vercel.json").exists():
                warnings.append("Non-Node project targeted for Vercel without vercel.json")

        if not artifact_expl:
            artifact_expl.append("Standard repository artifacts verified")

        dim_scores["artifacts"] = artifact_score
        dim_explanations["artifacts"] = "; ".join(artifact_expl)

        # Dimension 3: Environment & Secrets Hygiene (25 points)
        env_score = 25
        env_expl: list[str] = []
        secret_found = False

        if workspace_path.exists() and workspace_path.is_dir():
            for root, dirs, files in os.walk(workspace_path):
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "__pycache__", ".forgeops")]
                for f in files:
                    if f.endswith((".py", ".js", ".ts", ".json", ".yaml", ".yml", ".env", ".env.local")):
                        fp = Path(root) / f
                        try:
                            content = fp.read_text(encoding="utf-8", errors="ignore")
                            for pattern in self.SUSPICIOUS_SECRET_PATTERNS:
                                if pattern.search(content):
                                    rel_file = fp.relative_to(workspace_path).as_posix()
                                    blockers.append(f"Hardcoded sensitive secret pattern detected in {rel_file}")
                                    secret_found = True
                                    break
                            if secret_found:
                                break
                        except Exception:
                            pass
                if secret_found:
                    break

        if secret_found:
            env_score = 0
            env_expl.append("Sensitive secrets detected in committed files")
        else:
            if (workspace_path / ".env.example").exists():
                env_expl.append(".env.example documented")
            else:
                env_expl.append("No secrets detected")

        dim_scores["environment"] = env_score
        dim_explanations["environment"] = "; ".join(env_expl)

        # Dimension 4: Workload Integrity & Entrypoint (25 points)
        workload_score = 25
        workload_expl: list[str] = []

        if detected_stack.startswith("nodejs"):
            pkg_path = workspace_path / "package.json"
            if pkg_path.exists():
                try:
                    data = json.loads(pkg_path.read_text(encoding="utf-8"))
                    scripts = data.get("scripts", {})
                    if "start" in scripts or "build" in scripts or "preview" in scripts:
                        workload_expl.append(f"Lifecycle scripts verified: {list(scripts.keys())}")
                    else:
                        workload_score = 15
                        warnings.append("package.json lacks standard 'start' or 'build' scripts")
                        workload_expl.append("Missing standard start script")
                except Exception:
                    workload_score = 10
        elif detected_stack.startswith("python"):
            has_entry = any(
                (workspace_path / candidate).exists()
                for candidate in ("main.py", "app.py", "wsgi.py", "asgi.py", "src/main.py", "src/app.py")
            )
            if has_entry:
                workload_expl.append("Standard Python entrypoint file found")
            else:
                workload_score = 15
                warnings.append("No standard Python entrypoint (main.py/app.py) identified")
                workload_expl.append("Non-standard entrypoint")
        else:
            workload_expl.append("Generic workload structure verified")

        dim_scores["workload_integrity"] = workload_score
        dim_explanations["workload_integrity"] = "; ".join(workload_expl)

        # Aggregate total score
        total_score = sum(dim_scores.values())

        # Determine status
        if blockers:
            status = "unready"
        elif total_score >= 80:
            status = "ready"
        elif total_score >= 50:
            status = "needs_remediation"
        else:
            status = "unready"

        # Baseline delta calculations
        score_delta: int | None = None
        resolved_blockers: list[str] = []
        new_blockers: list[str] = list(blockers)

        if baseline_checkpoint is not None:
            if baseline_checkpoint.score is not None:
                score_delta = total_score - baseline_checkpoint.score

            base_blockers = set(baseline_checkpoint.blockers or [])
            curr_blockers = set(blockers)
            resolved_blockers = sorted(list(base_blockers - curr_blockers))
            new_blockers = sorted(list(curr_blockers - base_blockers))

        explanation_parts = [
            f"Readiness score: {total_score}/100 ({status.upper()})",
            f"Stack: {detected_stack}",
            f"Strategy: {strat_str}",
        ]
        if blockers:
            explanation_parts.append(f"{len(blockers)} blocker(s): {'; '.join(blockers)}")
        if warnings:
            explanation_parts.append(f"{len(warnings)} warning(s)")

        explanation = ". ".join(explanation_parts)

        return ReadinessCheckResult(
            checkpoint_type=checkpoint_type,
            assessment_version=assessment_version,
            status=status,
            score=total_score,
            max_score=100,
            score_delta=score_delta,
            selected_strategy=strat_str,
            detected_stack=detected_stack,
            snapshot_identity=snapshot_identity,
            dimension_scores=dim_scores,
            dimension_explanations=dim_explanations,
            blockers=blockers,
            warnings=warnings,
            artifacts_detected=detected_artifacts,
            resolved_blockers=resolved_blockers,
            new_blockers=new_blockers,
            explanation=explanation,
            assessment_error=None,
        )


# ---------------------------------------------------------------------------
# Atomic Persistence Protocol (Connection-Level Savepoint)
# ---------------------------------------------------------------------------


def _extract_checkpoint_payload(
    run_id: uuid.UUID,
    data: ReadinessCheckResult | ReadinessCheckpoint | dict[str, Any],
) -> dict[str, Any]:
    """Normalizes input checkpoint result or object into insertable dictionary."""
    if isinstance(data, ReadinessCheckResult):
        raw = data.to_dict()
    elif isinstance(data, ReadinessCheckpoint):
        raw = {
            "checkpoint_type": data.checkpoint_type,
            "assessment_version": data.assessment_version,
            "status": data.status,
            "score": data.score,
            "max_score": data.max_score,
            "score_delta": data.score_delta,
            "selected_strategy": data.selected_strategy,
            "detected_stack": data.detected_stack,
            "snapshot_identity": data.snapshot_identity,
            "dimension_scores": data.dimension_scores,
            "dimension_explanations": data.dimension_explanations,
            "blockers": data.blockers,
            "warnings": data.warnings,
            "artifacts_detected": data.artifacts_detected,
            "resolved_blockers": data.resolved_blockers,
            "new_blockers": data.new_blockers,
            "explanation": data.explanation,
            "assessment_error": data.assessment_error,
        }
    elif isinstance(data, dict):
        raw = dict(data)
    else:
        raise TypeError(f"Unsupported checkpoint payload type: {type(data)}")

    raw["run_id"] = run_id
    if "id" not in raw or raw["id"] is None:
        raw["id"] = uuid.uuid4()
    if "assessment_version" not in raw or raw["assessment_version"] is None:
        raw["assessment_version"] = 1
    if "max_score" not in raw or raw["max_score"] is None:
        raw["max_score"] = 100

    raw["checkpoint_type"] = normalize_checkpoint_type(str(raw.get("checkpoint_type", "initial_r0")))

    # Normalize status for database constraint check (CHECK (status IN ('completed', 'failed')))
    raw_status = str(raw.get("status", "completed")).lower()
    if raw_status in ("failed", "assessment_failed") or raw.get("assessment_error"):
        raw["status"] = "failed"
    else:
        raw["status"] = "completed"

    return raw


async def save_readiness_checkpoint(
    session: AsyncSession,
    run_id: uuid.UUID,
    checkpoint: ReadinessCheckResult | ReadinessCheckpoint | dict[str, Any],
) -> ReadinessCheckpoint:
    """Persists readiness checkpoint with connection-level savepoint, atomic idempotency, and cache protection.

    Guarantees:
    1. Preserves caller-owned pending state (session.new, session.dirty, session.deleted)
       by executing savepoint and insert at connection level rather than session.begin_nested().
    2. Protects cached ORM state: raises ReadinessDirtyCacheConflictError if a dirty checkpoint
       with identical (run_id, checkpoint_type, assessment_version) exists in session.dirty.
    3. Detects uq_run_checkpoint_version constraint violations using driver diagnostics.
    4. On conflict: rolls back savepoint, queries winning record, and returns winning record if semantically
       equivalent, or raises ReadinessCheckpointConflictError if payloads differ.
    5. Re-raises unrelated database integrity errors or non-integrity exceptions.
    """
    payload = _extract_checkpoint_payload(run_id, checkpoint)
    chk_type = payload["checkpoint_type"]
    chk_ver = payload["assessment_version"]

    # Step 1: Guard cached ORM state against dirty modifications
    for dirty_obj in session.dirty:
        if isinstance(dirty_obj, ReadinessCheckpoint):
            if (
                dirty_obj.run_id == run_id
                and dirty_obj.checkpoint_type == chk_type
                and dirty_obj.assessment_version == chk_ver
            ):
                raise ReadinessDirtyCacheConflictError(
                    f"Session contains dirty/unflushed modifications to checkpoint "
                    f"for run {run_id}, type {chk_type}, version {chk_ver}.",
                    run_id=run_id,
                    checkpoint_type=chk_type,
                    assessment_version=chk_ver,
                )

    # Step 2: Establish connection-level savepoint to bypass SQLAlchemy's pre-savepoint flush
    conn = await session.connection()
    savepoint = await conn.begin_nested()

    insert_stmt = insert(ReadinessCheckpoint.__table__).values(
        id=payload["id"],
        run_id=payload["run_id"],
        checkpoint_type=payload["checkpoint_type"],
        assessment_version=payload["assessment_version"],
        status=payload["status"],
        score=payload["score"],
        max_score=payload["max_score"],
        score_delta=payload.get("score_delta"),
        selected_strategy=payload["selected_strategy"],
        detected_stack=payload["detected_stack"],
        snapshot_identity=payload["snapshot_identity"],
        dimension_scores=payload["dimension_scores"],
        dimension_explanations=payload["dimension_explanations"],
        blockers=payload["blockers"],
        warnings=payload["warnings"],
        artifacts_detected=payload["artifacts_detected"],
        resolved_blockers=payload["resolved_blockers"],
        new_blockers=payload["new_blockers"],
        explanation=payload["explanation"],
        assessment_error=payload.get("assessment_error"),
    )

    try:
        await conn.execute(insert_stmt)
        await savepoint.commit()
    except IntegrityError as exc:
        # Determine if this integrity error is our targeted uniqueness constraint
        if not is_uq_checkpoint_version_violation(exc):
            # Unrelated integrity error (foreign key, check constraint, etc.)
            with contextlib.suppress(Exception):
                await savepoint.rollback()
            raise exc

        # Roll back the savepoint cleanly, retaining any rollback errors
        try:
            await savepoint.rollback()
        except Exception as rb_exc:
            raise rb_exc from exc

        # Reload the winning record without flushing outer session state
        with session.no_autoflush:
            stmt = select(ReadinessCheckpoint).where(
                ReadinessCheckpoint.run_id == run_id,
                ReadinessCheckpoint.checkpoint_type == chk_type,
                ReadinessCheckpoint.assessment_version == chk_ver,
            )
            result = await session.execute(stmt)
            winning_record = result.scalars().first()

        if winning_record is None:
            raise ReadinessVisibilityError(
                f"Concurrent checkpoint conflict occurred on (run_id={run_id}, "
                f"type={chk_type}, version={chk_ver}), but winning record is not visible."
            ) from exc

        # Compare payloads for semantic equivalence
        winning_dict = {
            "checkpoint_type": winning_record.checkpoint_type,
            "assessment_version": winning_record.assessment_version,
            "status": winning_record.status,
            "score": winning_record.score,
            "max_score": winning_record.max_score,
            "score_delta": winning_record.score_delta,
            "selected_strategy": winning_record.selected_strategy,
            "detected_stack": winning_record.detected_stack,
            "snapshot_identity": winning_record.snapshot_identity,
            "dimension_scores": winning_record.dimension_scores,
            "dimension_explanations": winning_record.dimension_explanations,
            "blockers": winning_record.blockers,
            "warnings": winning_record.warnings,
            "artifacts_detected": winning_record.artifacts_detected,
            "resolved_blockers": winning_record.resolved_blockers,
            "new_blockers": winning_record.new_blockers,
            "explanation": winning_record.explanation,
            "assessment_error": winning_record.assessment_error,
        }

        if is_payload_semantically_equivalent(payload, winning_dict):
            # Idempotent match: return winning record safely
            return winning_record

        raise ReadinessCheckpointConflictError(
            f"Concurrent checkpoint conflict on run {run_id}, type {chk_type}, "
            f"version {chk_ver}: attempted payload differs from stored checkpoint.",
            existing_checkpoint=winning_record,
            run_id=run_id,
            checkpoint_type=chk_type,
            assessment_version=chk_ver,
        ) from exc
    except Exception as exc:
        with contextlib.suppress(Exception):
            await savepoint.rollback()
        raise exc

    # Step 3: Load or populate the ORM object in caller's session without autoflush
    with session.no_autoflush:
        orm_obj = await session.get(ReadinessCheckpoint, payload["id"])
        if orm_obj is None:
            stmt = select(ReadinessCheckpoint).where(ReadinessCheckpoint.id == payload["id"])
            res = await session.execute(stmt)
            orm_obj = res.scalars().first()

    if orm_obj is None:
        raise RuntimeError(f"Inserted checkpoint {payload['id']} could not be loaded into ORM session.")

    return orm_obj


def save_readiness_checkpoint_sync(
    session: Session,
    run_id: uuid.UUID,
    checkpoint: ReadinessCheckResult | ReadinessCheckpoint | dict[str, Any],
) -> ReadinessCheckpoint:
    """Synchronous version of save_readiness_checkpoint for sync Session environments."""
    payload = _extract_checkpoint_payload(run_id, checkpoint)
    chk_type = payload["checkpoint_type"]
    chk_ver = payload["assessment_version"]

    for dirty_obj in session.dirty:
        if isinstance(dirty_obj, ReadinessCheckpoint):
            if (
                dirty_obj.run_id == run_id
                and dirty_obj.checkpoint_type == chk_type
                and dirty_obj.assessment_version == chk_ver
            ):
                raise ReadinessDirtyCacheConflictError(
                    f"Session contains dirty/unflushed modifications to checkpoint "
                    f"for run {run_id}, type {chk_type}, version {chk_ver}.",
                    run_id=run_id,
                    checkpoint_type=chk_type,
                    assessment_version=chk_ver,
                )

    conn = session.connection()
    savepoint = conn.begin_nested()

    insert_stmt = insert(ReadinessCheckpoint.__table__).values(
        id=payload["id"],
        run_id=payload["run_id"],
        checkpoint_type=payload["checkpoint_type"],
        assessment_version=payload["assessment_version"],
        status=payload["status"],
        score=payload["score"],
        max_score=payload["max_score"],
        score_delta=payload.get("score_delta"),
        selected_strategy=payload["selected_strategy"],
        detected_stack=payload["detected_stack"],
        snapshot_identity=payload["snapshot_identity"],
        dimension_scores=payload["dimension_scores"],
        dimension_explanations=payload["dimension_explanations"],
        blockers=payload["blockers"],
        warnings=payload["warnings"],
        artifacts_detected=payload["artifacts_detected"],
        resolved_blockers=payload["resolved_blockers"],
        new_blockers=payload["new_blockers"],
        explanation=payload["explanation"],
        assessment_error=payload.get("assessment_error"),
    )

    try:
        conn.execute(insert_stmt)
        savepoint.commit()
    except IntegrityError as exc:
        if not is_uq_checkpoint_version_violation(exc):
            with contextlib.suppress(Exception):
                savepoint.rollback()
            raise exc

        try:
            savepoint.rollback()
        except Exception as rb_exc:
            raise rb_exc from exc

        with session.no_autoflush:
            stmt = select(ReadinessCheckpoint).where(
                ReadinessCheckpoint.run_id == run_id,
                ReadinessCheckpoint.checkpoint_type == chk_type,
                ReadinessCheckpoint.assessment_version == chk_ver,
            )
            result = session.execute(stmt)
            winning_record = result.scalars().first()

        if winning_record is None:
            raise ReadinessVisibilityError(
                f"Concurrent checkpoint conflict occurred on (run_id={run_id}, "
                f"type={chk_type}, version={chk_ver}), but winning record is not visible."
            ) from exc

        winning_dict = {
            "checkpoint_type": winning_record.checkpoint_type,
            "assessment_version": winning_record.assessment_version,
            "status": winning_record.status,
            "score": winning_record.score,
            "max_score": winning_record.max_score,
            "score_delta": winning_record.score_delta,
            "selected_strategy": winning_record.selected_strategy,
            "detected_stack": winning_record.detected_stack,
            "snapshot_identity": winning_record.snapshot_identity,
            "dimension_scores": winning_record.dimension_scores,
            "dimension_explanations": winning_record.dimension_explanations,
            "blockers": winning_record.blockers,
            "warnings": winning_record.warnings,
            "artifacts_detected": winning_record.artifacts_detected,
            "resolved_blockers": winning_record.resolved_blockers,
            "new_blockers": winning_record.new_blockers,
            "explanation": winning_record.explanation,
            "assessment_error": winning_record.assessment_error,
        }

        if is_payload_semantically_equivalent(payload, winning_dict):
            return winning_record

        raise ReadinessCheckpointConflictError(
            f"Concurrent checkpoint conflict on run {run_id}, type {chk_type}, "
            f"version {chk_ver}: attempted payload differs from stored checkpoint.",
            existing_checkpoint=winning_record,
            run_id=run_id,
            checkpoint_type=chk_type,
            assessment_version=chk_ver,
        ) from exc
    except Exception as exc:
        with contextlib.suppress(Exception):
            savepoint.rollback()
        raise exc

    with session.no_autoflush:
        orm_obj = session.get(ReadinessCheckpoint, payload["id"])
        if orm_obj is None:
            stmt = select(ReadinessCheckpoint).where(ReadinessCheckpoint.id == payload["id"])
            res = session.execute(stmt)
            orm_obj = res.scalars().first()

    if orm_obj is None:
        raise RuntimeError(f"Inserted checkpoint {payload['id']} could not be loaded into ORM session.")

    return orm_obj
