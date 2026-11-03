# System Specification: GitHub Repository Synchronization & Vercel Cloud Deployment

**Document ID**: SPEC-2026-10-07-CLOUD-DEPLOY
**Status**: APPROVED
**Author**: Antigravity Platform Engineering
**Scope**: GitHub Repository Management (List, Create Public/Private, Push) and Direct Vercel Cloud Deployment

---

## 1. Executive Summary & Requirements

ForgeOps currently deploys workloads locally to Docker Engine, Docker Compose, and Kubernetes clusters via the Go agent. This specification introduces direct integration with **GitHub** and **Vercel**, allowing operators to:

1. **Push Local Workspaces to GitHub**:
   - Select an existing GitHub repository (both public and private repositories accessible by the user's GitHub credential).
   - Alternatively, create a brand-new repository directly on GitHub with a configurable visibility flag (**Public** or **Private**).
   - Push the local project codebase from the host machine to GitHub without exposing or persisting GitHub tokens in `.git/config` on disk.
2. **Deploy to Vercel**:
   - Authenticate with Vercel using a Personal or Team Access Token stored in the Vault.
   - Detect project framework (Next.js, Vite, React, static HTML, Svelte, Vue, or generic Node.js).
   - Automatically determine if `vercel.json` configuration is required (e.g., for single-page applications or custom rewrites) or if native Vercel zero-config applies.
   - Dispatch a deployment payload directly via the Vercel REST API (`POST https://api.vercel.com/v13/deployments`) and stream the resulting live `*.vercel.app` production/preview URL back to the ForgeOps dashboard.

---

## 2. Architecture & Data Flow

```
+===================================================================================================+
| 1. DASHBOARD CLIENT (Next.js 16 / React 19 Frontend)                                              |
|                                                                                                   |
|    [ Cloud Deploy Drawer / Modal ]                                                                |
|    +------------------------------------------+  +----------------------------------------------+ |
|    | Tab A: GitHub Synchronization            |  | Tab B: Vercel Deployment                     | |
|    | - Radio: "Existing Repo" | "Create New"  |  | - Framework detection badge                  | |
|    | - Visibility: [Public] | [Private]       |  | - Vercel Project Name input                  | |
|    | - Branch Name (default: main)            |  | - Optional vercel.json generator             | |
|    | - Action: "Push to GitHub"               |  | - Action: "Deploy to Vercel"                 | |
|    +------------------------------------------+  +----------------------------------------------+ |
+===================================================================================================+
                 |                                                        |
                 | POST /api/v1/projects/{id}/github/push                 | POST /api/v1/projects/{id}/vercel/deploy
                 v                                                        v
+===================================================================================================+
| 2. CONTROL PLANE GATEWAY (FastAPI Backend)                                                        |
|                                                                                                   |
|    A. GitHub Integration Subsystem (`backend/src/integrations/`)                                  |
|       - Reads GitHub Token from `github_account_links` / Vault                                    |
|       - `GET /api/v1/integrations/github/repositories`: Queries GitHub REST API (all visibility)  |
|       - `POST /api/v1/integrations/github/repositories`: Creates repo via GitHub API              |
|       - `POST /api/v1/projects/{id}/github/push`: Injects token into signed envelope for agent    |
|                                                                                                   |
|    B. Vercel Integration Subsystem (`backend/src/integrations/vercel/`)                           |
|       - Reads VERCEL_TOKEN from Vault                                                             |
|       - Detects framework from codebase AST inventory                                             |
|       - Bundles deployable assets / triggers Vercel REST API v13 deployment                       |
|       - Returns live `https://<project>.vercel.app` preview URL                                   |
+===================================================================================================+
                 |
                 | Ed25519 Signed Envelope over Persistent WebSocket
                 v
+===================================================================================================+
| 3. EXECUTION PLANE (Go 1.23 Agent Daemon)                                                         |
|                                                                                                   |
|    `git.push` Dispatcher:                                                                         |
|    - Runs in project's `workspace_root`                                                           |
|    - Initializes git repository if uninitialized (`git init -b <branch>`)                        |
|    - Sets remote origin to `https://github.com/<owner>/<repo>.git`                                |
|    - Commits staged workspace: `git add . && git commit -m "<msg>"`                              |
|    - Pushes securely: `git -c http.extraheader="<AUTH_HEADER>" push -u origin`                     |
|    - ZERO cleartext secrets are stored in `.git/config` on the host machine                       |
+===================================================================================================+
```

---

## 3. GitHub Integration Specification

### 3.1 Repository Creation API

- **Endpoint**: `POST /api/v1/integrations/github/repositories`
- **Request Body**:
  ```json
  {
    "name": "my-portfolio",
    "description": "Deployed via ForgeOps",
    "private": true
  }
  ```
- **Backend Processing**:
  - Validates active GitHub link/token for the principal.
  - Calls GitHub API: `POST https://api.github.com/user/repos` with headers:
    - `AUTH_HEADER: <BEARER_SCHEME> <TOKEN>`
    - `Accept: application/vnd.github+json`
  - Returns `RepositoryItem` with `clone_url`, `html_url`, `default_branch`, `private`.

### 3.2 Git Push Dispatch & Agent Execution

- **Endpoint**: `POST /api/v1/projects/{project_id}/github/push`
- **Request Body**:
  ```json
  {
    "repo_url": "https://github.com/parag8487/my-portfolio.git",
    "branch": "main",
    "commit_message": "Deploy from ForgeOps"
  }
  ```
- **Agent Operations (`git.push`)**:
  1. Checks if `.git` directory exists. If not, runs `git init -b <branch>`.
  2. Sets git user config if unconfigured (`git config user.name "ForgeOps Deployer"`).
  3. Stages all project files: `git add -A`.
  4. Commits changes: `git commit -m "<commit_message>"` (ignores error if nothing to commit).
  5. Sets remote: `git remote remove origin 2>/dev/null; git remote add origin <repo_url>`.
  6. Dispatches push using in-memory header:
     ```bash
     git -c http.extraheader="<AUTH_HEADER>" push -u origin <branch>
     ```
  7. Returns stdout/stderr log summary and commit SHA.

---

## 4. Vercel Cloud Deployment Specification

### 4.1 Token Storage

- The operator sets `VERCEL_TOKEN` in the ForgeOps Vault (`POST /api/v1/projects/{id}/secrets` with key `VERCEL_TOKEN` or globally in `provider_credentials`).

### 4.2 Framework Detection & Configuration (`vercel.json`)

- The backend inspects the scanned project framework:
  - **Next.js**: Zero configuration required. Vercel natively executes `next build`.
  - **Vite / React SPA**: Adds single-page rewrite to avoid 404s on refresh:
    ```json
    {
      "rewrites": [{ "source": "/(.*)", "destination": "/index.html" }]
    }
    ```
  - **Static HTML / CSS**: Deployed as static file directory.
  - If `vercel.json` already exists in workspace, it is preserved and prioritized.

### 4.3 Deployment Trigger API

- **Endpoint**: `POST /api/v1/projects/{project_id}/vercel/deploy`
- **Request Body**:
  ```json
  {
    "project_name": "my-portfolio",
    "vercel_token": "optional_override_or_reads_from_vault"
  }
  ```
- **Deployment Execution**:
  - Collects files from the project repository or requests the Go agent to bundle deployable files.
  - Calls Vercel API: `POST https://api.vercel.com/v13/deployments`.
  - Streams build status and returns the final `url` (e.g. `https://my-portfolio-theta.vercel.app`) and inspection link.

---

## 5. Security & Threat Modeling

1. **Credential Isolation**:
   - Neither GitHub nor Vercel tokens are ever returned to the client in plaintext.
   - When the agent performs `git push`, the token is passed strictly as a CLI flag parameter in memory (`-c http.extraheader`), so `.git/config` on disk holds only the clean HTTPS URL without credentials.
2. **Replay & Command Injection Defense**:
   - The push command is dispatched as a signed Ed25519 envelope through the governance chokepoint.
   - Repository URLs and branch names are sanitized using strict alphanumeric regex patterns to prevent shell command injection.
