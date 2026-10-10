# ForgeOps Autonomous Deployment: GitHub Direct Push & Pull Request Publishing Specification

**Document Version:** 1.0.0  
**Date:** 2026-10-10  
**Status:** Approved for Implementation Planning  
**Target Branch:** `phase-2-implementation`

---

## 1. Overview & Objectives

ForgeOps Autonomous Deployment Orchestrator currently publishes release synchronization commits directly to a configured branch (`target_branch`) during the `STAGE_GITHUB_RELEASE` operational stage. This specification extends the orchestrator to support two explicit publishing modes for GitHub-enabled deployment strategies (`docker_github_vercel`, `docker_github`, `github_only`):

1. **Direct Push (`direct_push`):** Pushes release commits directly to the selected target branch.
2. **Create Pull Request (`pull_request`):** Pushes release commits to a deterministic run-scoped source branch (`forgeops/deploy-{run_id}`), never directly touching the base branch, and creates or reconciles an open pull request targeting the selected base branch.

### Architectural Principles
- **Durable & Idempotent:** All branch and PR operations must be safe across worker retries, crash recovery, and transient provider timeouts without generating duplicate PRs or orphan unverified state.
- **Strict Server-Side Validation:** The backend must independently verify repository access, branch existence, permissions, and parameters; never trusting raw client input.
- **Backward Compatible:** Legacy runs and stored configurations without an explicit `publishing_mode` default strictly to `direct_push`.
- **Zero Secret Exposure:** Sealed GitHub credentials decrypted strictly in-memory; no credentials or tokens are ever persisted into database stage metadata or client responses.
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
     - Resolves canonical `default_branch` (e.g. `main`, `master`, `trunk`).
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

#### Error Mapping
- **404 Not Found:** Returned uniformly when GitHub answers 404 (nonexistent repository or inaccessible private repository), preventing user identity/repo enumeration.
- **403 Forbidden / 429 Too Many Requests:** Returned as `502 Bad Gateway` with clear error detail if GitHub rate limits are exhausted.
- **502 Bad Gateway:** Returned on upstream network or protocol exceptions.

---

### 2.2. Durable Configuration Schema Updates

Update [`GitHubConfigRequest`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_schemas.py#L65) in [`backend/src/deployments/autonomous_schemas.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/deployments/autonomous_schemas.py):

```python
class GitHubPublishingMode(str, Enum):
    DIRECT_PUSH = "direct_push"
    PULL_REQUEST = "pull_request"


class GitHubConfigRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repository_mode: str = Field(default="existing", max_length=50)
    repository_name: str = Field(
        ...,
        min_length=3,
        max_length=200,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
    )
    target_branch: str = Field(..., min_length=1, max_length=255)
    base_branch: str | None = Field(default=None, max_length=255)
    publishing_mode: GitHubPublishingMode = Field(default=GitHubPublishingMode.DIRECT_PUSH)
    commit_message: str | None = Field(default="Automated deployment by ForgeOps", max_length=500)
    pr_title: str | None = Field(default=None, max_length=255)
    pr_body: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def reconcile_and_normalize_branches(self) -> Self:
        # Reconcile base_branch and target_branch if both are provided
        if self.base_branch is not None and self.base_branch.strip():
            if self.target_branch and self.target_branch.strip() != self.base_branch.strip():
                raise ValueError("Contradictory branch configuration: 'target_branch' and 'base_branch' must match if both are specified")
            self.target_branch = self.base_branch.strip()
        self.base_branch = self.target_branch
        return self
```

---

## 3. Worker Execution & Idempotent PR Synchronization

### 3.1. Stage Execution: `STAGE_GITHUB_RELEASE`

When the worker encounters `STAGE_GITHUB_RELEASE`, it inspects `run.configuration["github_config"]`.

#### Mode A: `direct_push`
1. Fetches current ref from `GET /repos/{owner}/{repo}/git/ref/heads/{target_branch}`.
2. Pushes deployment payload commit directly to `target_branch`.
3. Verifies that the commit was created, records `commit_sha`, and persists metadata:
   ```json
   {
     "publishing_mode": "direct_push",
     "repository": "owner/repo",
     "target_branch": "main",
     "commit_sha": "abc1234",
     "live": true
   }
   ```

#### Mode B: `pull_request`
1. **Deterministic Source Branch Identification:**
   - Source branch: `source_branch = f"forgeops/deploy-{run.id}"`
   - Base branch: `base_branch = config.target_branch`
2. **Base Ref Resolution:**
   - Calls `GET /repos/{owner}/{repo}/git/ref/heads/{base_branch}` to obtain `base_sha`.
   - If base branch is missing, halts immediately with `status = "failed"` (`Base branch does not exist`).
3. **Source Branch Reconciliation & Creation:**
   - Queries `GET /repos/{owner}/{repo}/git/ref/heads/{source_branch}`.
   - **Case 1 (Branch does not exist - 404):**
     - Creates ref `refs/heads/{source_branch}` pointing to `base_sha` via `POST /repos/{owner}/{repo}/git/refs`.
     - Handles potential concurrency race: If creation returns 422 (already exists), re-queries `GET /repos/{owner}/{repo}/git/ref/heads/{source_branch}` to validate the winning remote ref.
   - **Case 2 (Branch exists - 200):**
     - Inspects current remote SHA (`existing_sha`).
     - If `existing_sha == base_sha` or matches recorded head SHA from previous attempt, reuse safely.
     - If branch diverged unexpectedly, halts with a conflict error rather than force-pushing or overwriting unexpected commits.
4. **Source Branch Commit Push:**
   - Pushes deployment payload targeting `branch: source_branch`.
   - Captures `commit_sha`.
5. **Idempotent PR Creation & Reconciliation:**
   - Before calling PR creation, queries existing pull requests:
     `GET /repos/{owner}/{repo}/pulls?head={owner}:{source_branch}&base={base_branch}&state=all`
   - **Case A (Matching PR found):**
     - If `pr.state == "open"`: Adopts `pr_number`, `pr_url`, observed `state`, and observed `merged`.
     - If `pr.state == "closed"` or `pr.merged == true`: Reports observed terminal state and stops with a conflict error.
   - **Case B (No matching PR found):**
     - Calls `POST /repos/{owner}/{repo}/pulls`:
       ```json
       {
         "title": "feat(deploy): autonomous deployment run <run_id_prefix>",
         "body": "Automated deployment pull request generated by ForgeOps...\nRun ID: <run_id>",
         "head": "forgeops/deploy-<run_id>",
         "base": "<base_branch>"
       }
       ```
     - Handles ambiguous timeouts: If call times out, refetches open PR list before reporting failure.
     - Handles duplicate PR error (422): If GitHub indicates PR already exists, re-queries and adopts the matching PR.
6. **Persisted Stage Metadata:**
   ```json
   {
     "publishing_mode": "pull_request",
     "repository": "owner/repo",
     "target_branch": "main",
     "base_branch": "main",
     "source_branch": "forgeops/deploy-<run_id>",
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
   - Queries and records dynamic `merged` field from provider response (never hardcoded).
4. Marks `target_results["github"] = "verified"` only if all checks pass.

---

## 5. Frontend User Experience & Components

### 5.1. Launch Modal: [`AutonomousDeployModal.tsx`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/frontend/features/deployments/AutonomousDeployModal.tsx)

- **Repository Selection:** Combobox featuring linked repositories from `GET /api/v1/integrations/github/repositories` plus freeform custom `owner/repo` input.
- **Debounced Branch Discovery:**
  - 300ms debounce on repository input.
  - Reset branch selection when repository changes.
  - Queries `GET /api/v1/integrations/github/repositories/{owner}/{repo}/branches`.
  - Automatically identifies and pre-selects `default_branch`, displaying a distinct badge (`Default`) in the selector.
  - Displays clear status for loading (`Loader2`), empty list, 404/inaccessible, and rate limits.
  - Disables form submission until repository access and branch are validated.
- **Publishing Mode Radios:**
  - Rendered when strategy includes GitHub.
  - Radio options:
    - **Direct branch push:** "Push the release commit directly to the selected branch."
    - **Create pull request:** "Push changes to source branch `forgeops/deploy-<run-id>` and open a pull request targeting this branch."
  - Dynamic label:
    - Direct push: *"Target Branch"*
    - Pull request: *"Base Branch (Destination for PR)"*

### 5.2. Execution Dashboard: [`JenkinsPipelineDashboard.tsx`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/frontend/features/deployments/JenkinsPipelineDashboard.tsx)

- **Operational Artifacts Section:**
  - If `publishing_mode === "pull_request"`, displays the **Pull Request Card**:
    - PR Number & Status badge (`Open` / `Merged` / `Closed`).
    - Clickable PR URL opening GitHub in a new tab.
    - Branch flow pill: `source_branch` $\rightarrow$ `base_branch`.
    - Commit SHA link to GitHub commit.
  - If `publishing_mode === "direct_push"`, maintains existing target branch and commit SHA display.
  - Fully hydrated from persisted stage metadata, surviving page reloads and worker handoffs.

---

## 6. Testing & Quality Assurance Plan

### 6.1. Unit & Schema Tests
- Validation of `GitHubConfigRequest`:
  - Rejection of conflicting `target_branch` and `base_branch`.
  - Rejection of invalid repository format.
  - Defaulting legacy configs to `direct_push`.

### 6.2. Staging Integration Tests (Mocked Upstream)
- Branch discovery route pagination and error responses (404, 403, 429).
- Safe source branch creation and branch race recovery.
- Source branch divergence conflict detection.
- Partial failure recovery: push succeeds, PR creation times out, subsequent retry adopts existing branch and PR without duplicate creation.
- Rejection of closed/merged existing PRs.
- G7 verification evaluating PR head/base/state alignment.

### 6.3. Live Provider Integration Tests (Disposable Infrastructure)
- Target repository: `parag8487/test-forgeops`.
- Test `test_live_github_direct_push_publishing`: verifies direct commit and G7 verification.
- Test `test_live_github_pull_request_publishing`:
  - Creates source branch `forgeops/deploy-{run_id}`.
  - Opens real PR on GitHub targeting `main`.
  - Validates PR URL, PR number, and open state.
  - G7 verifies remote head commit and open PR.
- Clean boundary tests: missing credentials safely halt without synthesizing cloud artifacts.
