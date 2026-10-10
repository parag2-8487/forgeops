# ForgeOps Autonomous Deployment: GitHub Direct Push & Pull Request Publishing Specification

**Document Version:** 1.3.0  
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
Adheres strictly to `git-check-ref-format --branch`:
1. Cannot be empty or whitespace-only.
2. UTF-8 byte length must not exceed 255 bytes.
3. Cannot start with `/` or `.`, and cannot end with `/`, `.`, or `.lock`.
4. Cannot contain consecutive slashes `//`.
5. Cannot contain `..`, `@{`, `\\`, or be a single `@`.
6. Cannot contain ASCII control characters (0x00–0x1F, 0x7F) or any of: ` `, `~`, `^`, `:`, `?`, `*`, `[`.
7. No path component between `/` can start with `.` or end with `.lock`.

**Positive Examples:** `main`, `master`, `develop`, `feature/oauth-login`, `release/v2.1.0`, `bugfix/issue-1234.v2`, `user/alice/work`  
**Negative Examples:** `.hidden`, `feature//login`, `feature/`, `/release`, `v1.0.`, `feature.lock`, `feature/sub.lock/task`, `feat:bug`, `feat?x`, `feat*all`, `feat[1]`, `feat~1`, `feat^2`, `feat..1`, `a@{b`, `feat branch`, `feat\branch`, `   `

```python
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Self
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_git_branch_name(branch: str | None) -> str | None:
    """Validates branch name format against git ref naming rules."""
    if branch is None:
        return None
    b = branch.strip()
    if not b:
        raise ValueError("Branch name cannot be empty or whitespace-only.")
    if len(b.encode("utf-8")) > 255:
        raise ValueError("Branch name exceeds maximum length of 255 bytes.")
    if b == "@" or b.startswith("/") or b.endswith("/") or b.endswith("."):
        raise ValueError(f"Invalid branch name '{b}': cannot start/end with '/' or end with '.'.")
    if "//" in b:
        raise ValueError(f"Invalid branch name '{b}': consecutive slashes '//' are forbidden.")
    if ".." in b or "@{" in b or "\\" in b:
        raise ValueError(f"Invalid branch name '{b}': contains forbidden sequence ('..', '@{{', or '\\').")
    for ch in b:
        code = ord(ch)
        if code < 32 or code == 127 or ch in " ~^:?*[":
            raise ValueError(f"Invalid branch name '{b}': contains forbidden character '{ch}'.")
    for comp in b.split("/"):
        if not comp:
            raise ValueError(f"Invalid branch name '{b}': empty path component.")
        if comp.startswith("."):
            raise ValueError(f"Invalid branch name '{b}': path component '{comp}' cannot start with '.'.")
        if comp.endswith(".lock"):
            raise ValueError(f"Invalid branch name '{b}': path component '{comp}' cannot end with '.lock'.")
    return b


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
   - Manifest Bytes: `b"ForgeOps Autonomous Deployment Run 835783a4-7c13-4c00-a233-90034f3a1db7\nCreated: 2026-10-10T14:00:00Z\n"` (length: 96 bytes)
   - Calculated SHA-256 Digest: `0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913`
2. **Durable Operation Intent Recording:**
   Before making any remote mutating Git API calls, the worker records an `operation_intent` payload into `stage_metadata` and flushes the database session:
   ```json
   {
     "operation_intent": {
       "payload_digest": "0ff6b151b520e538ddd7fa41f169cacf74012de54f84540581e9a450bc8a3913",
       "manifest_path": "forgeops-autonomous-deploy.txt",
       "target_branch": "main",
       "base_sha": "def5678"
     }
   }
   ```
   This ensures that any recovery process immediately has the exact expected digest, path, and base SHA.

#### URL Encoding Rules for Git Refs and PRs
All GitHub API paths and query parameters involving branch names MUST be URL-encoded:
- Git Ref paths: `/repos/{owner}/{repo}/git/ref/heads/{quote(branch, safe='')}`
- Contents API: `/repos/{owner}/{repo}/contents/{path}?ref={quote(branch, safe='')}`
- Pull Requests query: `head={owner}:{quote(source_branch, safe='')}&base={quote(base_branch, safe='')}`

---

### 3.2. Direct-Push Execution, Concurrency & Crash Recovery

#### Normal Execution with Optimistic Concurrency
1. Resolves `target_branch`.
2. Queries `GET /repos/{owner}/{repo}/git/ref/heads/{quote(target_branch, safe='')}` to verify existence and capture `base_sha`.
3. Checks if `forgeops-autonomous-deploy.txt` already exists on `target_branch` via `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(target_branch, safe='')}` to obtain `existing_blob_sha` if present.
4. Generates deterministic manifest bytes and `payload_digest`.
5. Calls GitHub Contents API `PUT /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt`:
   - Passes `branch: target_branch`, `content: base64(manifest_bytes)`, `message: f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"`.
   - Passes `sha: existing_blob_sha` if the file existed.
   - **Optimistic Concurrency Control:** If another writer updated `target_branch` between steps 2 and 5, GitHub rejects the request with `409 Conflict`. The worker halts with `ConflictError: "Target branch '{target_branch}' was modified concurrently on remote."` It never force-pushes or overwrites foreign changes.
6. Captures resulting `commit_sha` and persists metadata:
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
If the worker crashed after pushing the commit but before persisting stage metadata:
1. Worker queries the current manifest file at tip:
   `GET /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt?ref={quote(target_branch, safe='')}`
2. **If Current Tip File Content Matches `payload_digest`:**
   - Worker queries commit history specifically for this file path:
     `GET /repos/{owner}/{repo}/commits?path=forgeops-autonomous-deploy.txt&sha={quote(target_branch, safe='')}&per_page=1`
   - Verifies that:
     1. Commit message matches `f"feat(deploy): autonomous deployment run {str(run.id)[:8]}"`.
     2. Commit ancestry roots at `base_sha`.
   - If verified: The commit was successfully pushed by this run. Worker adopts this `commit_sha` and proceeds without creating a duplicate commit.
3. **If Current Tip File Content Does NOT Match `payload_digest`:**
   - The file at tip was modified by a later commit or our commit never landed.
   - Worker queries the file's commit history:
     `GET /repos/{owner}/{repo}/commits?path=forgeops-autonomous-deploy.txt&sha={quote(target_branch, safe='')}&per_page=10`
   - If a commit matching our message and `payload_digest` exists in the branch ancestry: adopts that `commit_sha`.
   - If NOT found in ancestry: halts with `ConflictError: "Expected deployment commit was not found on target branch."` Never force-pushes or overwrites unrelated changes.

---

### 3.3. Pull Request Execution & Commit-Level Recovery

#### Deterministic Setup
- Source branch: `source_branch = f"forgeops/deploy-{run.id}"`
- Base branch: `base_branch = config.target_branch`
- Generates deterministic manifest bytes and `payload_digest`.

#### Base Branch Verification
- Queries `GET /repos/{owner}/{repo}/git/ref/heads/{quote(base_branch, safe='')}` to capture `base_sha`.
- If base branch is missing, halts immediately with `status = "failed"` (`Base branch does not exist`).

#### Source Branch Reconciliation & Provenance Verification
Worker queries `GET /repos/{owner}/{repo}/git/ref/heads/{quote(source_branch, safe='')}`:
- **Scenario 1: Branch Does Not Exist (404):**
  - Creates `refs/heads/{source_branch}` pointing to `base_sha` via `POST /repos/{owner}/{repo}/git/refs`.
  - Handles concurrent creation race: if 422 returned, refetches the branch ref.
  - Pushes deployment payload commit to `source_branch`. Captures resulting `commit_sha`.
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
    - Worker pushes deployment payload commit normally to `source_branch`, capturing `commit_sha`.
  - **Sub-case 2c (Source Branch Diverged / Unrelated Foreign Commit):**
    - `tip_sha != base_sha` and fails the rigorous provenance check.
    - Worker aborts with `ConflictError: "Source branch 'forgeops/deploy-{run.id}' diverged unexpectedly on remote."` Never force-pushes or overwrites foreign commits.
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
2. Verifies branch head or commit ancestry includes `commit_sha` on `target_branch`.
3. If commit exists on a different branch or is unverified, marks `target_results["github"] = "failed"`.

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
     - Note: Source branch ref may have been deleted post-merge by GitHub; G7 does not fail because `refs/heads/{source_branch}` is missing.
     - Asserts `merge_commit_sha is not None`.
     - Verifies `merge_commit_sha` exists in repository and is reachable on `target_branch` (verified via `GET /repos/{owner}/{repo}/commits/{merge_commit_sha}`).
     - Asserts `observed_head_sha == commit_sha` (the PR's merged head matches the run commit).
     - `target_results["github"] = "verified"`
     - Stage metadata records `pr_state = "closed"`, `pr_merged = true`, `merge_commit_sha = merge_commit_sha`.
   - **Case 3: PR Was Closed Without Merge (`observed_state == "closed"` and `observed_merged == false`):**
     - Target failed: The PR was rejected or abandoned.
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
- Validation of `GitHubConfigRequest`:
  - Rejection of misspelled / extra fields via `extra="forbid"`.
  - Rejection of whitespace-only and invalid git ref characters (`..`, `~`, `^`, `:`, `?`, `*`, `[`, `\`, leading/trailing `/`).
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
- Direct-push crash recovery:
  - Push succeeds, worker crashes before stage metadata commit, retry adopts existing commit via payload digest check without duplicating commit or force-pushing.
  - Optimistic concurrency conflict detection when target branch advances concurrently.
- Pull request crash recovery:
  - Push succeeds, worker crashes, retry adopts source-branch commit via provenance verification.
  - Base branch advancing after branch creation does not falsely flag source branch divergence.
  - Source branch divergence conflict detection when foreign commits exist.
  - Partial failure recovery: push succeeds, PR creation times out, subsequent retry adopts existing branch and PR without duplicate creation.
- G7 verification evaluating:
  - Open PR (`state == "open"`, `head.sha == commit_sha`) -> `verified`.
  - Open PR with head SHA mismatch -> `failed`.
  - Merged PR (`state == "closed"`, `merged == true`, merge commit on base) -> `verified`.
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
