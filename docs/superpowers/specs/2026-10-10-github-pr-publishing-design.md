# ForgeOps Autonomous Deployment: GitHub Direct Push & Pull Request Publishing Specification

**Document Version:** 1.1.0  
**Date:** 2026-10-10  
**Status:** Draft — Pending Review  
**Target Branch:** `phase-2-implementation`

---

## 1. Overview & Objectives

ForgeOps Autonomous Deployment Orchestrator currently publishes release synchronization commits directly to a configured branch during the `STAGE_GITHUB_RELEASE` operational stage. This specification extends the orchestrator to support two explicit publishing modes for GitHub-enabled deployment strategies (`docker_github_vercel`, `docker_github`, `github_only`):

1. **Direct Push (`direct_push`):** Pushes release commits directly to the selected target branch.
2. **Create Pull Request (`pull_request`):** Pushes release commits to a deterministic run-scoped source branch (`forgeops/deploy-{run_id}`), never directly modifying the base branch, and creates or reconciles an open pull request targeting the selected base branch.

### Architectural Principles
- **Durable & Idempotent:** All branch, commit, and PR operations must be safe across worker retries, crash recovery, and transient provider timeouts without duplicating commits or creating multiple PRs.
- **Strict Server-Side Validation:** The backend must independently verify repository access, branch existence, permissions, and parameters; never trusting raw client input. Incoming API requests reject unrecognized fields (`extra="forbid"`).
- **Dynamic Branch Resolution:** `target_branch` is optional for new requests. If omitted, the backend dynamically resolves the repository's actual default branch via GitHub API, never hardcoding `main`.
- **Backward Compatible:** Legacy runs and stored configurations without an explicit `publishing_mode` default strictly to `direct_push`. Legacy stored configurations parse gracefully without failing on historical shapes.
- **Zero Secret Exposure:** Sealed GitHub credentials are decrypted strictly in-memory; no credentials or tokens are ever persisted into database stage metadata, error details, or client responses.
- **Non-Conflating Verification:** Creation of an open pull request is distinct from merging. G7 verification evaluates the active PR state and head commit without assuming or forcing a merge.

---

## 2. API Endpoints & Wire Schemas

### 2.1. Paginated Branch and Permission Discovery Endpoint

Add a dedicated endpoint to [`backend/src/integrations/routes.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/integrations/routes.py):

- **Path:** `GET /api/v1/integrations/github/repositories/{owner}/{repo}/branches`
- **Method:** `GET`
- **Security:** `require_principal` dependency + unsealed token derived from [`GitHubLinkService.usable_token`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/integrations/service.py#L383).
- **Upstream GitHub Calls:**
  1. `GET https://api.github.com/repos/{owner}/{repo}`:
     - Validates repository existence and access.
     - Resolves canonical `default_branch` (e.g. `main`, `master`, `develop`, `trunk`).
     - Extracts `permissions.push` as an informational capability hint (`can_push`).
  2. `GET https://api.github.com/repos/{owner}/{repo}/branches?per_page=100&page={page}`:
     - Paginates up to 10 pages (bounded at 1,000 branches).
     - Reports `truncated: true` if the repository exceeds the 1,000 branch threshold.

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
- **Insufficient Permissions (403 non-rate-limit):** If GitHub responds with 403 and message indicates permission or scope denial, returns `403 Forbidden` with `problem("github-permission-denied", detail="Linked GitHub account lacks required read/write permissions for '{owner}/{repo}'.")`.
- **Rate Limit Exceeded (403/429 with rate limit headers):** If `x-ratelimit-remaining == "0"` or upstream status is 429, returns `429 Too Many Requests` (or `503 Service Unavailable`) with `problem("github-rate-limited", detail="GitHub API rate limit exceeded. Retry after {reset_time}.")`.
- **Upstream Network Errors:** `httpx.ConnectError` or `httpx.TimeoutException` returns `502 Bad Gateway` with `problem("github-upstream-unreachable", detail="Could not connect to GitHub API. Please retry.")`.
- **Malformed Upstream Response:** Invalid JSON or missing required fields returns `502 Bad Gateway` with `problem("github-upstream-invalid", detail="GitHub API returned an invalid response structure.")`.

---

### 2.2. Durable Configuration Schema & Strict Validation

Update [`GitHubConfigRequest`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_schemas.py#L65) in [`backend/src/deployments/autonomous_schemas.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_schemas.py):

```python
class GitHubPublishingMode(str, Enum):
    DIRECT_PUSH = "direct_push"
    PULL_REQUEST = "pull_request"


class GitHubConfigRequest(BaseModel):
    """Configuration for GitHub release synchronization and publishing.

    Incoming API requests forbid unknown fields to reject typos immediately.
    Stored database configurations are parsed permissively for backward compatibility.
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
    target_branch: str | None = Field(default=None, min_length=1, max_length=255)
    base_branch: str | None = Field(default=None, max_length=255)
    publishing_mode: GitHubPublishingMode = Field(default=GitHubPublishingMode.DIRECT_PUSH)
    commit_message: str | None = Field(default="Automated deployment by ForgeOps", max_length=500)
    pr_title: str | None = Field(default=None, max_length=255)
    pr_body: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def reconcile_and_normalize_branches(self) -> Self:
        # If both target_branch and base_branch are provided, they must be identical
        if self.base_branch is not None and self.base_branch.strip():
            b_norm = self.base_branch.strip()
            if self.target_branch is not None and self.target_branch.strip():
                t_norm = self.target_branch.strip()
                if t_norm != b_norm:
                    raise ValueError(
                        f"Contradictory branch configuration: 'target_branch' ({t_norm}) and "
                        f"'base_branch' ({b_norm}) must match if both are specified."
                    )
            self.target_branch = b_norm
        if self.target_branch is not None:
            self.target_branch = self.target_branch.strip()
            self.base_branch = self.target_branch
        return self
```

#### Dynamic Default Branch Resolution
In [`AutonomousDeploymentService.create_run`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_service.py#L110):
- If `request.github_config` has `target_branch is None`:
  1. Service queries GitHub API (`GET /repos/{owner}/{repo}`) using user's unsealed token.
  2. Resolves `default_branch` directly from the provider response (e.g. `develop` or `main`).
  3. Sets `github_config.target_branch = default_branch` and `github_config.base_branch = default_branch`.
  4. If repository access fails or repository is inaccessible, rejects creation with `400 Bad Request` or `404 Not Found`.
- Legacy runs with already persisted `target_branch` are preserved as-is without re-querying.

---

## 3. Worker Execution & Commit-Level Idempotency

### 3.1. Stage Execution: `STAGE_GITHUB_RELEASE`

When the worker enters `STAGE_GITHUB_RELEASE`, it inspects `run.configuration["github_config"]`.

#### Mode A: `direct_push`
1. Resolves `target_branch`.
2. Queries `GET /repos/{owner}/{repo}/git/ref/heads/{target_branch}` to verify existence and fetch `base_sha`.
3. Pushes deployment payload commit directly to `target_branch`.
4. Persists metadata:
   ```json
   {
     "publishing_mode": "direct_push",
     "repository": "owner/repo",
     "target_branch": "main",
     "base_sha": "def5678",
     "commit_sha": "abc1234",
     "live": true
   }
   ```

#### Mode B: `pull_request`
1. **Deterministic Branch & State Setup:**
   - Source branch: `source_branch = f"forgeops/deploy-{run.id}"`
   - Base branch: `base_branch = config.target_branch`
2. **Base Branch Verification:**
   - Calls `GET /repos/{owner}/{repo}/git/ref/heads/{base_branch}` to fetch `base_sha`.
   - Halts immediately if base branch does not exist on remote.
3. **Commit-Level Idempotency & Crash Recovery:**
   - Worker queries `GET /repos/{owner}/{repo}/git/ref/heads/{source_branch}`.
   - **Scenario 1: Branch Does Not Exist (404):**
     - Creates `refs/heads/{source_branch}` pointing to `base_sha` via `POST /repos/{owner}/{repo}/git/refs`.
     - Handles concurrent creation race: if 422 returned, refetches the branch ref.
     - Pushes deployment payload to `source_branch` via `PUT /repos/{owner}/{repo}/contents/forgeops-autonomous-deploy.txt`.
     - Captures resulting `commit_sha`.
   - **Scenario 2: Branch Already Exists (200):**
     - Inspects current remote tip (`current_remote_sha`).
     - Inspects the remote file content or commit history for `forgeops-autonomous-deploy.txt`:
       - **Sub-case 2a (Commit Already Pushed by Previous Attempt):** The remote commit on `source_branch` contains this run's unique ID (`run.id`) in the deployment manifest.
         - **Recovery Action:** Worker recognizes that the push succeeded before a previous crash/timeout. It adopts `current_remote_sha` as `commit_sha` without pushing a duplicate commit, without force-pushing, and without altering remote history.
       - **Sub-case 2b (Branch freshly created at base SHA):** `current_remote_sha == base_sha`.
         - **Recovery Action:** Pushes deployment payload commit normally, capturing `commit_sha`.
       - **Sub-case 2c (Branch Diverged / Unrelated Commit):** `current_remote_sha != base_sha` and does NOT match this run's deployment payload.
         - **Safety Guard:** Worker aborts with `WorkerFencingLostError` or `ConflictError`: `"Source branch 'forgeops/deploy-{run.id}' diverged unexpectedly on remote."` Never force-pushes or overwrites unknown commits.
4. **Idempotent PR Creation & Reconciliation:**
   - Before calling `POST /repos/{owner}/{repo}/pulls`, worker executes:
     `GET /repos/{owner}/{repo}/pulls?head={owner}:{source_branch}&base={base_branch}&state=all`
   - **If Matching PR Exists:**
     - Validates that `pr.head.ref == source_branch` and `pr.base.ref == base_branch`.
     - If `pr.state == "open"`: Adopts `pr_number`, `pr_url`, observed `state`, and observed `merged` (e.g. `false`).
     - If `pr.state == "closed"` or `pr.merged == true`: Reports observed terminal state and stops with a conflict error.
   - **If No Matching PR Exists:**
     - Calls `POST /repos/{owner}/{repo}/pulls` with:
       ```json
       {
         "title": "feat(deploy): autonomous deployment run <run_id_prefix>",
         "body": "Automated deployment pull request generated by ForgeOps Autonomous Deployment Orchestrator.\nRun ID: <run_id>",
         "head": "forgeops/deploy-<run_id>",
         "base": "<base_branch>"
       }
       ```
     - Handles timeouts: If POST times out, refetches open PR list before failing.
     - Handles 422 duplicate PR response: If GitHub indicates a PR already exists, queries and adopts the matching PR.
5. **Durable Metadata Persistence:**
   ```json
   {
     "publishing_mode": "pull_request",
     "repository": "owner/repo",
     "target_branch": "main",
     "base_branch": "main",
     "source_branch": "forgeops/deploy-<run_id>",
     "base_sha": "def5678",
     "commit_sha": "abc1234",
     "pr_number": 42,
     "pr_url": "https://github.com/owner/repo/pull/42",
     "pr_state": "open",
     "pr_merged": false,
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

### 4.2. Verification for `pull_request`
1. Verifies commit exists: `GET /repos/{owner}/{repo}/commits/{commit_sha}`.
2. Verifies source branch ref points to `commit_sha`: `GET /repos/{owner}/{repo}/git/ref/heads/{source_branch}`.
3. Verifies PR details: `GET /repos/{owner}/{repo}/pulls/{pr_number}`:
   - Asserts PR belongs to expected `repository`.
   - Asserts `head.ref == source_branch`.
   - Asserts `base.ref == target_branch`.
   - Asserts `state == "open"`.
   - Records observed `merged` boolean directly from provider payload (never hardcoded).
4. Marks `target_results["github"] = "verified"` only if all checks pass.

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
  - Rejection of conflicting `target_branch` and `base_branch`.
  - Rejection of invalid repository format.
  - Verification that omitted `target_branch` passes schema validation as `None` for dynamic service resolution.

### 6.2. Staging Integration Tests (Mocked Upstream)
- Branch discovery route pagination, `truncated` flag, and distinct error responses (404, 403 permission, 429 rate limit, 502 network).
- Dynamic default branch resolution when `target_branch` is omitted.
- Safe source branch creation and branch race recovery.
- Commit-level idempotency: push succeeds, worker crashes before stage metadata commit, retry recovers tip commit without duplicate push or force-push.
- Source branch divergence conflict detection.
- Partial failure recovery: push succeeds, PR creation times out, subsequent retry adopts existing branch and PR without duplicate creation.
- Rejection of closed/merged existing PRs.
- G7 verification evaluating PR head/base/state alignment.

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
