# ForgeOps Autonomous Deployment: GitHub Direct Push & Pull Request Publishing Specification

**Document Version:** 1.6.0  
**Date:** 2026-10-10  
**Status:** Draft — Pending Review  
**Target Branch:** `phase-2-implementation`

---

## 1. Overview & Objectives

ForgeOps Autonomous Deployment Orchestrator publishes release synchronization commits during the `STAGE_GITHUB_RELEASE` operational stage. This specification extends the orchestrator to support two explicit publishing modes for GitHub-enabled deployment strategies (`docker_github_vercel`, `docker_github`, `github_only`):

1. **Direct Push (`direct_push`):** Pushes release commits directly to the selected target branch.
2. **Create Pull Request (`pull_request`):** Pushes release commits to a deterministic run-scoped source branch (`forgeops/deploy-{run_id}`), never directly modifying the base branch, and creates or reconciles an open pull request targeting the selected base branch.

### Architectural Principles
- **Durable & Idempotent:** All branch, commit, and PR operations must be safe across worker retries, crash recovery, and transient provider timeouts without duplicating commits or creating multiple PRs.
- **Strict Server-Side Validation:** The backend must independently verify repository access, branch existence, permissions, and parameters; never trusting raw client input. Incoming API requests reject unrecognized fields (`extra="forbid"`).
- **Dynamic Branch Resolution:** `target_branch` is optional for new requests. If omitted, the backend dynamically resolves the repository's actual default branch via GitHub API, never hardcoding `main`.
- **Ref Provenance & Commit Identity:** Recovery verifies exact payload digests, commit ancestry, and PR head SHA rather than assuming branch existence, ref names, or manifest text alone proves ownership.
- **Backward Compatible Persistence:** Legacy stored configurations parse through a permissive compatibility schema (`extra="ignore"`), preserving existing target branches and defaulting missing `publishing_mode` to `direct_push`. Legacy runs with an omitted branch resolve dynamically to the repository's actual default branch, never silently assuming `main`.
- **RFC-Compliant URL Construction:** All branch names and repository identifiers are strictly validated and URL-encoded across Git refs and PR API paths.
- **Zero Secret Exposure:** Sealed GitHub credentials are decrypted strictly in-memory; no credentials or tokens are ever persisted into database stage metadata, error details, or client responses.
- **Accurate G7 Verification:** Evaluates actual remote provider evidence, distinguishing open PRs, merged PRs, and rejected PRs without equating PR creation with merging.

---

## 2. API Endpoints & Wire Schemas

### 2.1. Paginated Branch and Permission Discovery Endpoint

Add a dedicated endpoint to [`backend/src/integrations/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/integrations/routes.py):

- **Path:** `GET /api/v1/integrations/github/repositories/{owner}/{repo}/branches`
- **Method:** `GET`
- **Security:** `require_principal` dependency + unsealed token derived from [`GitHubLinkService.usable_token`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/integrations/service.py#L383).
- **Path Parameter Validation:** `{owner}` and `{repo}` must match `^[A-Za-z0-9_.-]+$` to prevent URL traversal or structure corruption.
- **Upstream GitHub Calls:**
  1. `GET https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}`:
     - Validates repository existence and access.
     - Resolves canonical `default_branch` (e.g. `main`, `master`, `develop`, `trunk`).
     - Extracts `permissions.push` as an informational capability hint (`can_push`).
  2. `GET https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/branches?per_page=100&page={page}`:
     - Paginates up to 10 pages (bounded at 1,000 branches).
     - Reports `truncated: true` if the repository exceeds the 1,000 branch threshold.
- **Guaranteed Default Branch Ingestion:**
  - If `default_branch` is not present in the paginated branch array (e.g., when the repository has >1,000 branches and the default branch falls outside the pagination window), the backend explicitly prepends or inserts `default_branch` into `branches` so the repository default remains immediately selectable.

#### Wire Schema: `RepositoryBranchesResponse`
```python
class RepositoryBranchesResponse(BaseModel):
    owner: str
    repo: str
    default_branch: str
    branches: list[str]
    can_push: bool
    is_private: bool
    truncated: bool = False
```

#### Provider Error Mapping & Redaction
Follows standard RFC 7807 `problem(status, code, detail=...)` conventions with redacting sensitive tokens:
- **Inaccessible or Nonexistent Repository (404):** Returns `404 Not Found` with `problem("github-repository-not-found", detail="Repository '{owner}/{repo}' was not found or is inaccessible with the linked account.")`.
- **Insufficient Permissions (403 non-rate-limit):** If GitHub responds with 403 and the payload indicates permission or scope denial, returns `403 Forbidden` with `problem("github-permission-denied", detail="Linked GitHub account lacks required read/write permissions for '{owner}/{repo}'.")`.
- **Rate Limit Exceeded (403/429 with rate limit headers):** If `x-ratelimit-remaining == "0"` or upstream status is 429, returns `429 Too Many Requests` (or `503 Service Unavailable`) with `problem("github-rate-limited", detail="GitHub API rate limit exceeded. Retry after {reset_time}.")`.
- **Upstream Network Errors:** `httpx.ConnectError` or `httpx.TimeoutException` returns `502 Bad Gateway` with `problem("github-upstream-unreachable", detail="Could not connect to GitHub API. Please retry.")`.
- **Malformed Upstream Response:** Invalid JSON or missing required fields returns `502 Bad Gateway` with `problem("github-upstream-invalid", detail="GitHub API returned an invalid response structure.")`.

---

### 2.2. Strict Request Validation vs. Legacy Deserialization

To prevent misspelled fields in new requests while guaranteeing backward compatibility for historical stored runs, the platform defines two distinct schema models in [`backend/src/deployments/autonomous_schemas.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_schemas.py):

#### Git Branch Ref Validation Rules (`validate_git_branch_name`)
Validates branch names against Git ref naming rules and remote Git provider constraints without silent input normalization:
1. **No Whitespace or Silent Normalization:** Cannot be empty, whitespace-only, or contain any leading, trailing, or internal whitespace characters (` ` `\t` `\n` `\r`). The validator rejects invalid whitespace with an error rather than silently modifying requested branch names via `strip()`.
2. **No Command-Line Option Injection:** Cannot begin with a hyphen `-` (e.g. `-main`, `-f`, `--branch`), preventing option injection into downstream Git or CLI tools.
3. **UTF-8 Byte Length Limit:** Total UTF-8 encoded length must not exceed 255 bytes (`len(branch.encode('utf-8')) <= 255`).
4. **Ref Boundary Constraints:** Cannot start with `/` or `.`, and cannot end with `/`, `.`, or `.lock`.
5. **No Consecutive Slashes:** Cannot contain `//` or empty path components.
6. **No Reserved Git Sequences:** Cannot contain `..`, `@{`, `\\`, or be a single `@`.
7. **No Control or Reserved Characters:** Cannot contain ASCII control characters (0x00–0x1F, 0x7F) or any of: `~`, `^`, `:`, `?`, `*`, `[`.
8. **Component Rules:** No path component separated by `/` can start with `.` or end with `.lock`.

**Authoritative Validation Hierarchy & Git Equivalence:**
- **Authoritative Server-Side Rule:** `validate_git_branch_name()` is the authoritative server-side gate executed across all API endpoints, request schemas, and stored stage hydration.
- **Intentional Divergence from Raw `git check-ref-format --branch`:**
  1. **Single `@` Character:** Raw `git check-ref-format --branch @` accepts `@` as a local CLI alias for `HEAD`. ForgeOps explicitly rejects `@` because GitHub ref APIs reject `refs/heads/@`.
  2. **Explicit Byte Boundary:** ForgeOps enforces an explicit 255 UTF-8 byte boundary limit, whereas raw `git check-ref-format` relies on host filesystem path length limits.
  3. **Strict Rejection Over Mutation:** Raw `git check-ref-format --branch` prints normalized names; ForgeOps strictly rejects malformed input without mutating user intent.
- **Differential Verification:** Test suites execute differential tests comparing `validate_git_branch_name()` against `git check-ref-format --branch` whenever the `git` binary is available, verifying parity across all test vectors with `@` documented as the single intentional policy divergence.

**Positive Examples:** `main`, `master`, `develop`, `feature/oauth-login`, `release/v2.1.0`, `bugfix/issue-1234.v2`, `user/alice/work`, `team-alpha/job-1`, `feature/ü-login`, `'a' * 255`  
**Negative Examples:** ` main`, `main `, `main\t`, `-main`, `-f`, `--branch`, `@`, `.hidden`, `feature//login`, `feature/`, `/release`, `v1.0.`, `feature.lock`, `feature/sub.lock/item`, `feat:bug`, `feat?x`, `feat*all`, `feat[1]`, `feat~1`, `feat^2`, `feat..1`, `a@{b`, `feat branch`, `feat\branch`, `   `, `'a' * 256`

```python
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Self
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_git_branch_name(branch: str | None) -> str | None:
    """Validates branch name format against git ref naming rules without silent normalization.

    Rejects branch names with leading/trailing or internal whitespace rather than mutating via strip().
    Enforces remote Git provider branch safety rules.
    """
    if branch is None:
        return None
    if not branch:
        raise ValueError("Branch name cannot be empty.")
    if any(ch.isspace() for ch in branch):
        raise ValueError(f"Invalid branch name '{branch}': whitespace is forbidden.")
    if branch.startswith("-"):
        raise ValueError(f"Invalid branch name '{branch}': cannot begin with a hyphen '-' (git option injection prevention).")
    encoded = branch.encode("utf-8")
    if len(encoded) > 255:
        raise ValueError(f"Branch name exceeds maximum length of 255 bytes (got {len(encoded)} bytes).")
    if branch == "@" or branch.startswith("/") or branch.endswith("/") or branch.endswith("."):
        raise ValueError(f"Invalid branch name '{branch}': cannot be '@', start/end with '/', or end with '.'.")
    if "//" in branch:
        raise ValueError(f"Invalid branch name '{branch}': consecutive slashes '//' are forbidden.")
    if ".." in branch or "@{" in branch or "\\" in branch:
        raise ValueError(f"Invalid branch name '{branch}': contains forbidden sequence ('..', '@{{', or '\\').")
    for ch in branch:
        code = ord(ch)
        if code < 32 or code == 127 or ch in "~^:?*[":
            raise ValueError(f"Invalid branch name '{branch}': contains forbidden character '{ch}'.")
    for comp in branch.split("/"):
        if not comp:
            raise ValueError(f"Invalid branch name '{branch}': empty path component.")
        if comp.startswith("."):
            raise ValueError(f"Invalid branch name '{branch}': path component '{comp}' cannot start with '.'.")
        if comp.endswith(".lock"):
            raise ValueError(f"Invalid branch name '{branch}': path component '{comp}' cannot end with '.lock'.")
    return branch


class GitHubPublishingMode(str, Enum):
    DIRECT_PUSH = "direct_push"
    PULL_REQUEST = "pull_request"


class GitHubConfigRequest(BaseModel):
    """Strict configuration model for incoming API requests.

    Rejects unknown or misspelled fields with 422 Unprocessable Entity.
    """

    model_config = ConfigDict(extra="forbid")

    repository_mode: str = Field(default="existing", max_length=50)
    repository_name: str = Field(
        ...,
        min_length=3,
        max_length=200,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    # Optional on creation: dynamically resolved to repository default branch if omitted
    target_branch: str | None = Field(default=None, max_length=255)
    base_branch: str | None = Field(default=None, max_length=255)
    publishing_mode: GitHubPublishingMode = Field(default=GitHubPublishingMode.DIRECT_PUSH)
    commit_message: str | None = Field(default="Automated deployment by ForgeOps", max_length=500)
    pr_title: str | None = Field(default=None, max_length=255)
    pr_body: str | None = Field(default=None, max_length=10000)

    @field_validator("target_branch", "base_branch")
    @classmethod
    def validate_branch_format(cls, v: str | None) -> str | None:
        return validate_git_branch_name(v)

    @model_validator(mode="after")
    def reconcile_and_normalize_branches(self) -> Self:
        if self.base_branch is not None:
            if self.target_branch is not None and self.target_branch != self.base_branch:
                raise ValueError(
                    f"Contradictory branch configuration: 'target_branch' ({self.target_branch}) and "
                    f"'base_branch' ({self.base_branch}) must match if both are specified."
                )
            self.target_branch = self.base_branch
        self.base_branch = self.target_branch
        return self
```

#### Legacy Stored Deserialization Schema: `StoredGitHubConfig`
```python
class StoredGitHubConfig(BaseModel):
    """Permissive configuration model for database hydration and recovery.

    Tolerates extra/deprecated fields from earlier runs and defaults missing publishing_mode to direct_push.
    Never assumes 'main' when target_branch is omitted.
    """

    model_config = ConfigDict(extra="ignore")

    repository_mode: str = "existing"
    repository_name: str
    target_branch: str | None = None
    base_branch: str | None = None
    publishing_mode: GitHubPublishingMode = GitHubPublishingMode.DIRECT_PUSH
    commit_message: str | None = "Automated deployment by ForgeOps"
    pr_title: str | None = None
    pr_body: str | None = None

    @model_validator(mode="after")
    def normalize_legacy(self) -> Self:
        if self.target_branch and not self.base_branch:
            self.base_branch = self.target_branch
        elif self.base_branch and not self.target_branch:
            self.target_branch = self.base_branch
        return self
```

#### Dynamic Default Branch Resolution
In [`AutonomousDeploymentService.create_run`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_service.py#L110):
- If `request.github_config.target_branch is None`:
  1. Service queries GitHub API (`GET /repos/{owner}/{repo}`) using user's unsealed token.
  2. Resolves `default_branch` directly from the provider response (e.g. `develop` or `main`).
  3. Sets `github_config.target_branch = default_branch` and `github_config.base_branch = default_branch`.
  4. If repository access fails or repository is inaccessible, rejects creation with `400 Bad Request` or `404 Not Found`.
- Legacy runs with an already persisted `target_branch` are preserved as-is. If a legacy run somehow has `target_branch is None`, recovery dynamically resolves the repository's default branch via GitHub API, never silently defaulting to `main`.

---

## 3. Worker Execution, Stable Payload Digest & Commit Idempotency

### 3.1. Stable Manifest Payload Generation & Operation Intent

To ensure the exact deployment manifest bytes and payload digest are completely stable across crashes, worker handoffs, and retries:
1. **Deterministic Manifest Definition:**
   The manifest content is constructed strictly from immutable database attributes established at run creation time (`run.id` and `run.created_at` formatted in ISO 8601 UTC):
   ```python
   def build_deployment_manifest(run_id: uuid.UUID, created_at: datetime) -> tuple[bytes, str]:
       ts_str = created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
       body = f"ForgeOps Autonomous Deployment Run {run_id}\nCreated: {ts_str}\n".encode("utf-8")
       digest = hashlib.sha256(body).hexdigest()
       return body, digest
   ```
   **Concrete Illustrated Digest Example:**
   - Run ID: `835783a4-7c13-4c00-a233-90034f3a1db7`
   - Created At: `2026-10-10T14:00:00Z`
   - Manifest Bytes: `b"ForgeOps Autonomous Deployment Run 835783a4-7c13-4c00-a233-90034f3a1db7\nCreated: 2026-10-10T14:00:00Z\n"`
   - Exact UTF-8 Byte Length: `102` bytes
   - Calculated SHA-256 Digest: `0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913`

   **Reproducible Programmatic Fixture Definition:**
   ```python
   SAMPLE_RUN_ID = uuid.UUID("835783a4-7c13-4c00-a233-90034f3a1db7")
   SAMPLE_CREATED_AT = datetime(2026, 10, 10, 14, 0, 0, tzinfo=UTC)
   SAMPLE_MANIFEST_BYTES, SAMPLE_PAYLOAD_DIGEST = build_deployment_manifest(SAMPLE_RUN_ID, SAMPLE_CREATED_AT)

   assert len(SAMPLE_MANIFEST_BYTES) == 102
   assert SAMPLE_PAYLOAD_DIGEST == "0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913"
   ```
2. **Durable Operation Intent Persistence via Dedicated Session & Worker Fencing:**
   A SQLAlchemy `session.flush()` merely buffers SQL statements into the active database transaction; if the worker process crashes before a commit, the uncommitted transaction is rolled back by PostgreSQL. Furthermore, executing `await session.commit()` on the worker's primary shared application session would commit all pending/dirty ORM objects across other entities.

   **Dedicated Isolated Session Mechanism:**
   To guarantee transactional isolation and prevent committing unrelated pending ORM modifications, ForgeOps persists `operation_intent` using a dedicated, standalone session context obtained from `async_session_factory()`:
   ```python
   async with async_session_factory() as intent_session:
       target_stage = await intent_session.get(DeploymentStage, stage.id)
       if target_stage:
           target_stage.stage_metadata = {
               **target_stage.stage_metadata,
               "operation_intent": {
                   "publishing_mode": publishing_mode.value,
                   "payload_digest": payload_digest,
                   "manifest_path": "forgeops-autonomous-deploy.txt",
                   "target_branch": target_branch,
                   "base_sha": base_sha,
                   "existing_blob_sha": existing_blob_sha,  # None if file absent
                   "source_branch": source_branch,          # for pull_request mode
                   "intent_committed_at": datetime.now(UTC).isoformat(),
               },
           }
           await intent_session.commit()
   # Synchronize the in-memory stage object on the worker's primary session
   await session.refresh(stage)
   ```

   **Worker Lease, Fencing & Cancellation Guarantees:**
   - **Pre-Intent Validation:** Prior to committing the operation intent, the worker asserts its lease and fencing token via the deployment heartbeat coordinator (`heartbeat_worker_lease`), ensuring another worker has not claimed the lease or fenced execution. It also verifies `run.status != "cancelled"`.
   - **Pre-Mutation Revalidation:** Immediately before dispatching the first mutating remote GitHub API call (after the intent transaction has committed), the worker performs a second mandatory check:
     1. Re-verifies worker fencing token and lease validity.
     2. Re-verifies `run.status != "cancelled"`. If cancelled while the intent was being written, the worker halts immediately without performing any remote mutation.

   **Crash Recovery Between Intent Commit and First Remote Mutation:**
   If the worker crashes after the dedicated transaction commit but before the first mutating GitHub API request is received or processed by GitHub:
   - The database contains the committed `operation_intent`, but remote GitHub state has not been modified.
   - When a recovering worker assumes the lease, it loads `operation_intent` from stage metadata.
   - Before attempting commit adoption or raising false divergence alarms, recovery queries remote GitHub state:
     - For `direct_push`: inspects `GET /repos/{owner}/{repo}/git/ref/heads/{quote(target_branch, safe='')}` and `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(target_branch, safe='')}`.
     - For `pull_request`: inspects `GET /repos/{owner}/{repo}/git/ref/heads/{quote(source_branch, safe='')}`.
   - If remote GitHub state remains in its pre-mutation state (`source_branch` does not exist for PR mode, or no commit matching this run ID exists on `target_branch` and the manifest blob SHA matches `existing_blob_sha`), recovery recognizes that the crash occurred prior to remote mutation.
   - Recovery re-validates worker fencing and cancellation, updates `intent_committed_at` via the dedicated session, and proceeds with the initial remote mutation cleanly without false divergence alarms.

#### URL Encoding Rules for Git Refs and PRs
All GitHub API paths and query parameters involving branch names MUST be URL-encoded:
- Git Ref paths: `/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='')}`
- Contents API: `/repos/{owner}/{repo}/contents/{path}?ref={quote(branch, safe='')}`
- Pull Requests query: `head={owner}:{quote(source_branch, safe='')}&base={quote(base_branch, safe='')}`

---

### 3.2. Direct-Push Execution, Concurrency & Crash Recovery

#### GitHub Contents API Concurrency Semantics
The GitHub Contents API (`PUT /repos/{owner}/{repo}/contents/{path}`) operates at the file blob level rather than taking an optimistic lock on the entire branch ref:
- **`sha` Parameter Role:** The `sha` parameter identifies the Git blob SHA of the file being overwritten (or is omitted if creating a new file). It does NOT reference or enforce the branch head commit SHA.
- **Concurrent Commits to Unrelated Files:** If another writer pushes commits to `target_branch` that do not modify `forgeops-autonomous-deploy.txt`, the file blob SHA remains unchanged. In this situation, GitHub's Contents API does NOT return a 409 conflict; instead, GitHub attaches the new commit to the latest remote branch tip, automatically advancing `target_branch`.
- **Concurrent Modifications to the Manifest File:** If another writer updates or creates `forgeops-autonomous-deploy.txt` on `target_branch` between ForgeOps reading `existing_blob_sha` and issuing the `PUT` request, the remote blob SHA changes. GitHub detects this mismatch and rejects the request with a conflict response (`409 Conflict` or `422 Unprocessable Entity`).
- **Concurrent Creation of Previously Absent File:** If ForgeOps attempts to create `forgeops-autonomous-deploy.txt` without a `sha` parameter, but another writer creates the file concurrently on `target_branch`, GitHub may return either `409 Conflict` or `422 Unprocessable Entity` (e.g. indicating missing `sha` for an existing path).
- **Concurrent Branch Deletion:** If `target_branch` was removed on remote, GitHub responds with `404 Not Found`.

#### Normal Execution & Reachability Verification After Concurrent Commits
1. Resolves `target_branch`.
2. Queries `GET /repos/{owner}/{repo}/git/ref/heads/{quote(target_branch, safe='')}` to capture initial `base_sha`.
3. Checks if `forgeops-autonomous-deploy.txt` already exists on `target_branch` via `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(target_branch, safe='')}`:
   - If file exists (200 OK): captures `existing_blob_sha = response["sha"]`.
   - If file does not exist (404 Not Found): sets `existing_blob_sha = None`.
4. Generates deterministic manifest bytes and `payload_digest`.
5. Durably commits `operation_intent` into PostgreSQL via the dedicated isolated session context (`async with async_session_factory() as intent_session:`) with `base_sha`, `existing_blob_sha`, and `payload_digest`.
6. Re-verifies worker fencing and cancellation status immediately prior to dispatching the remote request.
7. Issues `PUT /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt`:
   - Payload:
     ```json
     {
       "message": "feat(deploy): autonomous deployment run <run_id_prefix>",
       "content": "<base64_encoded_manifest_bytes>",
       "branch": "<target_branch>",
       "sha": "<existing_blob_sha>" // omitted if existing_blob_sha is None
     }
     ```
8. Evaluates response and verifies commit reachability:
   - **On Success (`201 Created` or `200 OK`):**
     - Extracts `commit_sha = response["commit"]["sha"]` and `parent_shas = [p["sha"] for p in response["commit"]["parents"]]`.
     - **Branch Reachability Verification (Allowing Subsequent Commits):**
       - Rather than requiring `commit_sha` to remain the immediate tip of `target_branch` (which would falsely fail if unrelated commits land immediately afterward), ForgeOps verifies reachability directly via the GitHub Compare API:
         `GET /repos/{owner}/{repo}/compare/{quote(commit_sha, safe='')}...{quote(target_branch, safe='')}`
       - If `behind_by == 0` and `status in ("ahead", "identical")`:
         - If `status == "identical"`: `commit_sha` is the current tip of `target_branch`.
         - If `status == "ahead"`: subsequent unrelated commits landed on `target_branch`, but our run's commit remains fully reachable in the branch ancestry.
         - Reachability is verified. ForgeOps records `base_sha` and `commit_sha` and proceeds.
       - If `behind_by > 0` or `status not in ("ahead", "identical")` (e.g. branch diverged or was reset), or if `target_branch` was deleted (404):
         - Halts with `ConflictError: "Commit '{commit_sha}' is not reachable on target branch '{target_branch}' (compare status: '{status}', behind_by: {behind_by})."`
         - Never force-pushes or overwrites unrelated changes.
   - **On Conflict Response (`409 Conflict` or `422 Unprocessable Entity`):**
     - A concurrent write occurred on remote. ForgeOps executes **Manifest Write Conflict Reconciliation**:
       1. Refetches current file metadata: `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(target_branch, safe='')}`.
       2. Refetches commit history for the manifest path using bounded pagination:
          `GET /repos/{owner}/{repo}/commits?path=forgeops-autonomous-deploy.txt&sha={quote(target_branch, safe='')}&per_page=30&page={page}` (paginating up to 5 pages / 150 commits).
       3. Evaluates candidate commits sequentially from newest to oldest against three stopping conditions:
          - **Stopping Condition 1 (Match Found):** If candidate commit matches run provenance (message `f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"`, payload digest, authenticated author/committer, and branch reachability via Compare API with `behind_by == 0` and `status in ("ahead", "identical")`): adopts `commit_sha`, records stage metadata, and marks `live: true`. Search halts immediately.
          - **Stopping Condition 2 (Base Boundary Reached):** If a commit with `sha == base_sha` is encountered or commit timestamp predates `intent_committed_at`: search halts immediately. Inspects tip manifest; if remote blob changed, halts with `ConflictError`; if untouched, updates `base_sha` in operation intent and retries.
          - **Stopping Condition 3 (Pagination Limit Exhausted):** If 150 commits (5 pages) are examined without finding a matching run commit or reaching `base_sha`: halts safely with `ConflictError: "Target branch '{target_branch}' advanced significantly without finding run commit or reaching base boundary."`
       4. Never retries a blind overwrite, never force-pushes, and never destroys foreign changes.
9. Persists completed stage metadata with `commit_sha`, `payload_digest`, and marks `live: true`:
   ```json
   {
     "publishing_mode": "direct_push",
     "repository": "owner/repo",
     "target_branch": "main",
     "base_sha": "def5678",
     "commit_sha": "abc1234",
     "payload_digest": "0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913",
     "live": true
   }
   ```

#### Crash Recovery & Idempotent Retry (Direct Push)
If the worker crashed after issuing the Contents API write but before persisting completed stage metadata:
1. Worker loads durable `operation_intent` from `stage_metadata` (`payload_digest`, `base_sha`, `existing_blob_sha`, `manifest_path`).
2. **Identifying the Exact Run Commit via Bounded Path History & Reachability:**
   - Worker initiates bounded, paginated commit history retrieval affecting the manifest path on `target_branch`:
     `GET /repos/{owner}/{repo}/commits?path=forgeops-autonomous-deploy.txt&sha={quote(target_branch, safe='')}&per_page=30&page={page}` (up to 5 pages, max 150 commits).
   - Worker evaluates candidate commits sequentially from newest to oldest against three deterministic stopping conditions:
     1. **Stopping Condition 1 — Exact Run Commit Match:**
        - Commit message matches `f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"`.
        - Author / committer matches the authenticated ForgeOps GitHub integration identity.
        - Manifest content at candidate commit matches `payload_digest` (verified via `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={commit["sha"]}`).
        - Commit reachability on `target_branch`: verified via GitHub Compare API `GET /repos/{owner}/{repo}/compare/{quote(candidate["sha"], safe='')}...{quote(target_branch, safe='')}` reporting `behind_by == 0` and `status in ("ahead", "identical")`. Subsequent unrelated commits (`status == "ahead"`) are explicitly permitted.
        - *Action:* The commit was successfully created on remote prior to the crash. Worker adopts `commit_sha = candidate["sha"]`, persists completed stage metadata via the dedicated session, and marks `live: true`. Search halts immediately; worker does not create a duplicate commit or invoke the Contents API again.
     2. **Stopping Condition 2 — Base Boundary Reached:**
        - If a commit is encountered with `sha == base_sha` or with a commit timestamp strictly earlier than `intent_committed_at`.
        - *Action:* Any commits at or preceding `base_sha` were created before this deployment run's operation intent was committed; the run's commit cannot exist deeper in the history. Search halts immediately without fetching further pages. Worker inspects the current manifest file at `target_branch` tip:
          - **Case A (Conflicting Manifest Changes from Another Writer):** If the file exists and its blob SHA differs from `existing_blob_sha` (and does not match `payload_digest`), foreign changes occurred. Worker halts safely with `ConflictError: "Target branch '{target_branch}' has conflicting manifest state."` Never retries a blind overwrite.
          - **Case B (No Commit Landed & Manifest Untouched):** If the current blob SHA still equals `existing_blob_sha` (or file remains absent):
            - If `target_branch` advanced with unrelated commits (`current_tip_sha != base_sha`), worker updates `base_sha = current_tip_sha` in `operation_intent` via the dedicated session, re-validates fencing and cancellation, and re-executes the Contents API call.
            - If `target_branch` has not moved (`current_tip_sha == base_sha`), worker re-validates fencing and cancellation, and re-executes the Contents API call using the recorded intent.
     3. **Stopping Condition 3 — Pagination Limit Exhausted (150 Commits / 5 Pages):**
        - If all 5 pages (150 commits) have been inspected without finding a matching run commit and without encountering `base_sha`.
        - *Action:* The branch has advanced extensively with rapid commit velocity beyond the bounded recovery window. To guarantee safety and prevent duplicate writes or blind overwrites, ForgeOps halts safely with `ConflictError: "Target branch '{target_branch}' advanced significantly (exceeded 150 commits / 5 pages) without finding run commit or reaching base boundary."` requiring operator review. Under no circumstances does ForgeOps ever force-push or overwrite unexpected commits.

---

### 3.3. Pull Request Execution & Commit-Level Recovery

#### Deterministic Setup
- Source branch: `source_branch = f"forgeops/deploy-{run.id}"`
- Base branch: `base_branch = config.target_branch`
- Generates deterministic manifest bytes and `payload_digest`.

#### Base Branch Verification
- Queries `GET /repos/{owner}/{repo}/git/ref/heads/{quote(base_branch, safe='')}` to capture `base_sha`.
- If base branch is missing, halts immediately with `status = "failed"` (`Base branch does not exist`).

#### Source Branch Reconciliation, Existing Manifest Handling & Provenance Verification
Worker queries `GET /repos/{owner}/{repo}/git/ref/heads/{quote(source_branch, safe='')}`:
- **Scenario 1: Branch Does Not Exist (404):**
  - Creates `refs/heads/{source_branch}` pointing to `base_sha` via `POST /repos/{owner}/{repo}/git/refs`.
  - Handles concurrent creation race: if 422 returned, refetches the branch ref.
  - **Handling Manifest Inherited from Base SHA:**
    Because `source_branch` is branched from `base_sha`, it inherits any existing repository files at `base_sha`. ForgeOps queries `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(source_branch, safe='')}`:
    - If file exists (200 OK): captures `existing_source_blob_sha = response["sha"]`, and includes `"sha": existing_source_blob_sha` in the Contents API `PUT` call targeting `source_branch`.
    - If file does not exist (404 Not Found): sets `existing_source_blob_sha = None` and omits the `sha` parameter in the `PUT` call.
  - Pushes deployment payload commit to `source_branch` via `PUT /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt` with `"branch": source_branch`. Captures resulting `commit_sha`.
- **Scenario 2: Branch Already Exists (200):**
  - Inspects current remote tip SHA (`tip_sha`).
  - **Sub-case 2a (Commit Already Pushed by Previous Attempt):**
    - Worker fetches commit details: `GET /repos/{owner}/{repo}/commits/{tip_sha}`.
    - **Rigorous Provenance Check:**
      1. Commit message == `f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"`.
      2. File change `forgeops-autonomous-deploy.txt` content matches `payload_digest`.
      3. Commit parent == `base_sha` (or parent chain roots at `base_sha`).
    - If all three match: Worker adopts `tip_sha` as `commit_sha` without pushing a duplicate commit and without force-pushing.
  - **Sub-case 2b (Branch Freshly Created at Base SHA):**
    - `tip_sha == base_sha`.
    - Worker queries `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(source_branch, safe='')}`: captures `existing_source_blob_sha` if 200 OK, or `None` if 404.
    - Worker pushes deployment payload commit to `source_branch` passing `"sha": existing_source_blob_sha` (if present) and captures `commit_sha`.
  - **Sub-case 2c (Source Branch Diverged / Unrelated Foreign Commit):**
    - `tip_sha != base_sha` and fails the rigorous provenance check.
    - Worker aborts with `ConflictError: "Source branch 'forgeops/deploy-{run.id}' diverged unexpectedly on remote."` Never force-pushes or overwrites foreign commits.
  - **Handling Write Conflicts on Source Branch (`409 Conflict` or `422 Unprocessable Entity`):**
    - If the Contents API write to `source_branch` returns 409 or 422:
      1. Worker refetches the source branch tip ref: `GET /repos/{owner}/{repo}/git/ref/heads/{quote(source_branch, safe='')}`.
      2. Executes the rigorous provenance check on the new tip commit.
      3. If the commit matches the run's provenance and payload digest, worker adopts `tip_sha` as `commit_sha`.
      4. If the remote state is ambiguous or foreign, worker halts with `ConflictError`. Never retries a blind overwrite and never force-pushes.
  - **Note on Base Branch Advancement:**
    - If `base_branch` on GitHub advanced to a new commit *after* `source_branch` was created, this does NOT constitute source branch divergence. The source branch remains validly based on `base_sha`, and GitHub handles base branch delta resolution in the PR.

#### Idempotent PR Creation & Reconciliation
Before calling `POST /repos/{owner}/{repo}/pulls`, worker executes:
`GET /repos/{owner}/{repo}/pulls?head={quote(owner, safe='')}:{quote(source_branch, safe='')}&base={quote(base_branch, safe='')}&state=all`

- **If Matching PR Exists:**
  - Validates `pr.head.ref == source_branch`, `pr.head.sha == commit_sha`, and `pr.base.ref == base_branch`.
  - **If `pr.state == "open"`:** Adopts `pr_number`, `pr_url`, observed `state = "open"`, and observed `merged = false`.
  - **If `pr.state == "closed"` and `pr.merged == true`:**
    - PR was already merged on GitHub.
    - Adopts `pr_number`, `pr_url`, observed `state = "closed"`, observed `merged = true`, and `merge_commit_sha`.
  - **If `pr.state == "closed"` and `pr.merged == false`:**
    - PR was closed without merging (abandoned or rejected).
    - Stops with `ConflictError: "Existing pull request #{pr.number} was closed without merging."` Does not silently create a duplicate PR or reopen without authorization.
- **If No Matching PR Exists:**
  - Calls `POST /repos/{owner}/{repo}/pulls`:
    ```json
    {
      "title": "feat(deploy): autonomous deployment run <run_id_prefix>",
      "body": "Automated deployment pull request generated by ForgeOps Autonomous Deployment Orchestrator.\nRun ID: <run_id>",
      "head": "forgeops/deploy-<run_id>",
      "base": "<base_branch>"
    }
    ```
  - Handles timeouts: If POST times out, queries `state=all` list before failing.
  - Handles 422 duplicate error: If GitHub reports a PR already exists, queries and adopts the matching PR.

#### Durable Metadata Persistence (`STAGE_GITHUB_RELEASE`)
```json
{
  "publishing_mode": "pull_request",
  "repository": "owner/repo",
  "target_branch": "main",
  "base_branch": "main",
  "source_branch": "forgeops/deploy-<run_id>",
  "base_sha": "def5678",
  "commit_sha": "abc1234",
  "payload_digest": "0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913",
  "pr_number": 42,
  "pr_url": "https://github.com/owner/repo/pull/42",
  "pr_state": "open",
  "pr_merged": false,
  "merge_commit_sha": null,
  "live": true
}
```

---

## 4. Strategy-Aware G7 Verification Gate

In [`backend/src/deployments/autonomous_gates.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_gates.py#L514), when `STAGE_G7_VERIFICATION` runs with live verification active:

### 4.1. Verification for `direct_push`
1. Verifies commit exists on GitHub: `GET /repos/{owner}/{repo}/commits/{commit_sha}`.
2. Verifies branch reachability via GitHub Compare API:
   `GET /repos/{owner}/{repo}/compare/{quote(commit_sha, safe='')}...{quote(target_branch, safe='')}`
   - **If `behind_by == 0` and `status in ("ahead", "identical")`:** Direct reachability on `target_branch` is established. Subsequent unrelated commits (`status == "ahead"`) are explicitly permitted because the run's commit remains safely in the branch history.
   - **If `behind_by > 0` or `status not in ("ahead", "identical")` (e.g. diverged or behind) or branch 404:** Marks `target_results["github"] = "failed"` with gate error `"Commit '{commit_sha}' is not reachable on target branch '{target_branch}'."`
3. Validates manifest file at `commit_sha` matches recorded `payload_digest`.
4. If all checks pass, marks `target_results["github"] = "verified"`.

### 4.2. Verification for `pull_request` (Handling Open, Merged, and Closed PRs)
1. **Source Commit Check:**
   - Verifies commit exists on GitHub: `GET /repos/{owner}/{repo}/commits/{commit_sha}`.
2. **PR State Inspection:**
   - Queries `GET /repos/{owner}/{repo}/pulls/{pr_number}`:
   - Asserts PR belongs to expected `repository`.
   - Asserts `head.ref == source_branch` and `base.ref == target_branch`.
   - Captures actual observed provider fields: `observed_state = pr["state"]`, `observed_merged = pr["merged"]`, `merge_commit_sha = pr.get("merge_commit_sha")`, and `observed_head_sha = pr.get("head", {}).get("sha")`.
3. **Outcome Evaluation:**
   - **Case 1: PR is Open (`observed_state == "open"`):**
     - Validates that `observed_head_sha == commit_sha`. If the source branch moved to an unexpected commit, fails verification.
     - Target verified: The deployment successfully published changes and opened the PR for review.
     - `target_results["github"] = "verified"`
     - Stage metadata records `pr_state = "open"`, `pr_merged = false`.
   - **Case 2: PR Was Already Merged (`observed_state == "closed"` and `observed_merged == true`):**
     - Target verified: Changes have successfully landed on the base branch.
     - Note: The source branch ref `refs/heads/{source_branch}` may have been deleted post-merge by GitHub or repository branch cleanup; G7 does not fail because the source branch ref is absent.
     - Asserts `observed_head_sha == commit_sha` (the PR's merged head matches the exact run commit).
     - **Provider Evidence Consistency Check:**
       - If `observed_merged == true` but `merge_commit_sha is None` (e.g. squash merge without merge_commit_sha reported, or upstream data inconsistency):
         Provider evidence is incomplete or inconsistent. G7 fails verification honestly:
         `target_results["github"] = "failed"`
         Gate error: `"Pull request #{pr_number} is reported merged by provider but missing merge_commit_sha."`
     - **Direct Branch Reachability Verification via GitHub Compare API:**
       - Merely querying `GET /repos/.../commits/{merge_commit_sha}` only proves the commit object exists in repository storage, not that it is an ancestor of `target_branch`.
       - To verify reachability on the target branch directly, G7 calls the GitHub Compare Commits API:
         `GET /repos/{owner}/{repo}/compare/{quote(merge_commit_sha, safe='')}...{quote(target_branch, safe='')}`
       - **Evaluating Compare Results:**
         - When `merge_commit_sha` is an ancestor of (or identical to) `target_branch`, `target_branch` contains all commits of `merge_commit_sha`. GitHub reports:
           - `status in ("ahead", "identical")`
           - `behind_by == 0` (meaning `target_branch` is 0 commits behind `merge_commit_sha`).
         - **If `behind_by == 0` and `status in ("ahead", "identical")`:**
           Direct branch reachability is established. The merge commit is verified to be present in `target_branch`'s history. Proceed to manifest content verification.
         - **If `behind_by > 0` or `status not in ("ahead", "identical")` (e.g. `"diverged"` or `"behind"`):**
           `target_branch` does NOT contain `merge_commit_sha`. The PR may have merged into an unexpected base or the target branch diverged. G7 fails verification honestly:
           `target_results["github"] = "failed"`
           Gate error: `"Merge commit '{merge_commit_sha}' is not reachable from target branch '{target_branch}' (compare status: '{status}', behind_by: {behind_by})."`
         - **If Compare API Returns 404 or Upstream Network Error:**
           Reachability cannot be established. G7 fails verification honestly:
           `target_results["github"] = "failed"`
           Gate error: `"Failed to verify reachability of merge commit '{merge_commit_sha}' on branch '{target_branch}': upstream comparison failed."`
      - **Manifest Content & Payload Digest Verification at Merge Commit:**
        - To guarantee that the deployment manifest is present and intact in the merged commit tree across all merge strategies (`merge`, `squash`, `rebase`), G7 retrieves:
          `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(merge_commit_sha, safe='')}`
        - Decodes content bytes, computes SHA-256 digest, and asserts parity against `payload_digest`.
        - **Handling Across Merge Strategies:**
          1. **Merge Commit (`merge` strategy - 2 parents):** The merge commit inherits tree modifications from the source branch; manifest content at `merge_commit_sha` matches `payload_digest`.
          2. **Squash Merge (`squash` strategy - 1 parent):** GitHub creates a single synthetic commit on `target_branch` containing the aggregated tree; manifest content at `merge_commit_sha` matches `payload_digest`.
          3. **Rebase Merge (`rebase` strategy - 1 parent per PR commit):** GitHub reapplies individual commits onto `target_branch`; manifest content at `merge_commit_sha` (the tip rebased commit) matches `payload_digest`.
        - **Evaluating Manifest Outcomes:**
          - **If Manifest Matches `payload_digest`:**
            Direct branch reachability and payload integrity are verified.
            `target_results["github"] = "verified"`
            Stage metadata records `pr_state = "closed"`, `pr_merged = true`, `merge_commit_sha = merge_commit_sha`.
          - **If Manifest Returns 404 (Not Found):**
            The manifest file is absent at `merge_commit_sha`. G7 fails verification honestly:
            `target_results["github"] = "failed"`
            Gate error: `"Manifest file 'forgeops-autonomous-deploy.txt' not found at merge commit '{merge_commit_sha}'."`
          - **If Manifest Digest Mismatches `payload_digest`:**
            The content at `merge_commit_sha` differs from the run's expected payload. G7 fails verification honestly:
            `target_results["github"] = "failed"`
            Gate error: `"Manifest content at merge commit '{merge_commit_sha}' does not match expected payload digest."`
          - **If GitHub API Returns 403, 422, or 502:**
            Manifest retrieval failed. G7 fails verification honestly:
            `target_results["github"] = "failed"`
            Gate error: `"Failed to retrieve manifest content at merge commit '{merge_commit_sha}': upstream error."`
   - **Case 3: PR Was Closed Without Merge (`observed_state == "closed"` and `observed_merged == false`):**
     - Target failed: The PR was rejected, abandoned, or closed without merging.
     - `target_results["github"] = "failed"`
     - Gate error: `"Pull request #{pr_number} was closed without being merged."`
     - Stage metadata records `pr_state = "closed"`, `pr_merged = false`.
4. Persists the exact observed provider evidence; never fabricates a merge state.

---

## 5. Frontend User Experience & Components

### 5.1. Launch Modal: [`AutonomousDeployModal.tsx`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/frontend/features/deployments/AutonomousDeployModal.tsx)

- **Repository Selection:** Combobox featuring linked repositories from `GET /api/v1/integrations/github/repositories` plus freeform custom `owner/repo` input.
- **Debounced Branch Discovery & Truncation Handling:**
  - 300ms debounce on repository input.
  - Queries `GET /api/v1/integrations/github/repositories/{owner}/{repo}/branches`.
  - Automatically identifies and pre-selects `default_branch`, displaying a distinct badge (`Default`) in the selector.
  - When `truncated === true`: Renders an informational banner:
    > *"Showing first 1,000 branches (truncated). If your target branch is not in the list, type its exact name."*
  - Allows typing a custom branch name when truncated, with backend performing existence validation.
  - Displays distinct, accessible UI states for loading (`Loader2`), empty list, 404/inaccessible, and rate limits.
  - Disables form submission until repository access and branch are validated.
- **Publishing Mode Radios:**
  - Rendered when strategy includes GitHub (`docker_github_vercel`, `docker_github`, `github_only`).
  - Radio options:
    - **Direct branch push:** "Push the release commit directly to the selected branch."
    - **Create pull request:** "Push changes to source branch `forgeops/deploy-<run-id>` and open a pull request targeting this branch."
  - Dynamic label:
    - Direct push: *"Target Branch"*
    - Pull request: *"Base Branch (Destination for PR)"*

### 5.2. Execution Dashboard: [`JenkinsPipelineDashboard.tsx`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/frontend/features/deployments/JenkinsPipelineDashboard.tsx)

- **Operational Artifacts Section:**
  - If `publishing_mode === "pull_request"`, displays the **Pull Request Card**:
    - PR Number & Status badge (`Open` / `Merged` / `Closed` based on observed GitHub response).
    - Clickable PR URL opening GitHub in a new tab.
    - Branch flow pill: `source_branch` $\rightarrow$ `base_branch`.
    - Commit SHA link to GitHub commit.
  - If `publishing_mode === "direct_push"`, maintains existing target branch and commit SHA display.
  - Fully hydrated from persisted stage metadata, surviving page reloads and worker handoffs.

---

## 6. Testing & Quality Assurance Plan

### 6.1. Unit & Schema Tests
- Deterministic Manifest Fixture:
  - Exact UTF-8 byte length (102 bytes) and calculated SHA-256 digest (`0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913`) for reproducible test fixture (`SAMPLE_RUN_ID`, `SAMPLE_CREATED_AT`).
- Git Branch Validation (`validate_git_branch_name`):
  - Strict whitespace rejection without silent normalization: raises `ValueError` on leading, trailing, tab, and newline whitespace without `strip()` mutation.
  - UTF-8 byte boundary enforcement: 255-byte branch succeeds, 256-byte branch raises `ValueError`.
  - Command option injection prevention: `-main`, `-f`, `--branch` raise `ValueError`.
  - Ref boundary and character constraints: `@`, `//`, `..`, `.lock`, control characters, and reserved symbols raise `ValueError`.
  - Differential verification against `git check-ref-format --branch` asserting identical acceptance/rejection across all test vectors, documenting `@` as the single intentional policy divergence.
- Validation of `GitHubConfigRequest`:
  - Rejection of misspelled / extra fields via `extra="forbid"`.
  - Rejection of conflicting `target_branch` and `base_branch`.
  - Rejection of invalid repository format.
  - Verification that omitted `target_branch` passes schema validation as `None` for dynamic service resolution.
- Validation of `StoredGitHubConfig`:
  - Successfully parses legacy rows with extra/deprecated fields.
  - Defaults missing `publishing_mode` to `direct_push`.
  - Retains `target_branch = None` when omitted, requiring dynamic default resolution.

### 6.2. Staging Integration Tests (Mocked Upstream)
- Branch discovery route:
  - 1,000 branch pagination and `truncated: true` flag.
  - Ensuring repository `default_branch` is included in `branches` even when outside first 1,000 branches.
  - Distinct error responses (404, 403 permission, 429 rate limit, 502 network).
- Isolated durable operation intent & worker fencing:
  - Dedicated session context (`async with async_session_factory() as intent_session:`) ensuring `operation_intent` commits without committing unrelated dirty ORM entities.
  - Pre-intent and pre-mutation worker lease fencing and cancellation revalidation.
  - Crash recovery when worker crashes between intent commit and first remote mutation: verifies pristine remote repository state at base SHA and executes intended mutation without false divergence alarms.
- Direct-push concurrency, conflict reconciliation & bounded crash recovery:
  - Unrelated commits on target branch: Contents API attaches commit to advanced branch tip; worker verifies commit reachability on `target_branch` via Compare API (`behind_by == 0`, `status == "ahead"`), allowing subsequent commits.
  - Manifest write conflict reconciliation: handles both `409 Conflict` and `422 Unprocessable Entity` (concurrent creation of previously absent file without `sha`).
  - Bounded recovery pagination: mocks up to 5 pages (`per_page=30`, max 150 commits) on manifest path.
    - Stopping Condition 1: match found on page 1-5 adopts commit and terminates pagination immediately without duplicate writes.
    - Stopping Condition 2: encountering `sha == base_sha` or timestamp earlier than `intent_committed_at` terminates pagination immediately without fetching further pages; re-attempts write if manifest untouched.
    - Stopping Condition 3: pagination limit exhausted after 150 commits raises `ConflictError` safely without blind overwrite or duplicate write.
- Pull request execution, existing manifest inheritance & crash recovery:
  - Existing manifest inheritance: when `source_branch` is branched from `base_sha` where manifest exists, queries source branch manifest blob SHA and supplies `existing_source_blob_sha` in Contents API `PUT`.
  - Source branch write conflict (409/422): verifies refetch of source branch tip and provenance reconciliation without force-pushing.
  - Push succeeds, worker crashes: retry adopts source-branch commit via provenance verification.
  - Base branch advancing after branch creation does not falsely flag source branch divergence.
  - Source branch divergence conflict detection when foreign commits exist.
  - Partial failure recovery: push succeeds, PR creation times out, subsequent retry adopts existing branch and PR without duplicate creation.
- G7 verification evaluating:
  - Direct push: commit reachable on `target_branch` via Compare API (`behind_by == 0`, `status in ("ahead", "identical")`), matching `payload_digest` -> `verified`.
  - Direct push: commit not reachable (`behind_by > 0` or status diverged) -> `failed`.
  - Open PR (`state == "open"`, `head.sha == commit_sha`) -> `verified`.
  - Open PR with head SHA mismatch -> `failed`.
  - Merged PR (`state == "closed"`, `merged == true`):
    - Reachable merge commit verified via GitHub Compare API (`status in ("ahead", "identical")`, `behind_by == 0`).
    - Manifest content and `payload_digest` verification at `merge_commit_sha` across `merge`, `squash`, and `rebase` strategies -> `verified`.
    - Missing manifest file at `merge_commit_sha` (404) -> `failed`.
    - Manifest content digest mismatch at `merge_commit_sha` -> `failed`.
    - Unreachable or diverged merge commit (`behind_by > 0` or status `"diverged"`) -> `failed`.
    - Missing `merge_commit_sha` when reported merged -> `failed`.
  - Closed unmerged PR (`state == "closed"`, `merged == false`) -> `failed`.

### 6.3. Live Provider Integration Tests (Disposable Infrastructure)
- Target repository: `parag8487/test-forgeops`.
- Base branch portability: dynamically resolve default branch from repository metadata; if `main` is asserted, explicitly verify prerequisite.
- Test `test_live_github_direct_push_publishing`: verifies direct commit and G7 verification on dynamically resolved default branch.
- Test `test_live_github_pull_request_publishing`:
  - Creates source branch `forgeops/deploy-{run_id}`.
  - Opens real PR on GitHub targeting dynamically resolved default branch.
  - Validates PR URL, PR number, and open state.
  - G7 verifies remote head commit and open PR.
- Clean boundary tests: missing credentials safely halt without synthesizing cloud artifacts.
