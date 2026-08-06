# Agent autonomy and file preservation

## Never delete documentation

- Never delete, move, rename, or truncate any `.md` file unless the user explicitly asks for that specific file.
- This applies with no exceptions to: the four authoritative root documents (`AI-Powered-DevOps-Platform-Complete-Technical-Research.md`, `PRD.md`, `Tech-Stack-Analysis.md`, `phases.md`), `README.md`, `PROGRESS.md`, everything under `docs/`, everything under `.antigravity/specs/`, everything under `.antigravity/steering/`, `LICENSE`, `agent/LICENSE`, `agent/NOTICE`, and every structural `README.md` that marks a future-phase directory.
- Cleaning up, tidying, reorganising, or "consolidating" is not permission to delete a markdown file. Editing content in place is fine when the task calls for it; removing the file is not.
- If a markdown file looks redundant, obsolete, or duplicated, report it and let the user decide. Never act on that judgement alone.
- Never delete a file to make a check pass. Fix the check or report the conflict.

## Run routine commands without asking

Run these without requesting confirmation, and do not narrate the intention to run them first:

- Read-only inspection: `git status`, `git diff`, `git log`, `git show`, `git ls-files`, `git rev-parse`, `git branch`, `git check-ignore`, `gh run view`, `gh run list`, `gh pr view`, `gh api` GET requests
- Build and verify: `go build`, `go test`, `go vet`, `golangci-lint run`, `pytest`, `ruff check`, `ruff format --check`, `npx eslint`, `tsc --noEmit`, `vitest --run`, `opa test`
- Repository checks: any `scripts/check-*.sh`, `scripts/tests/*`
- Make targets that are not destructive: `help`, `build`, `test`, `lint`, `bootstrap`, `init-env`, `sbom`, `verify-release`
- Secret scanning: `gitleaks detect`, `gitleaks protect`, including via its pinned Docker image
- Container inspection and local stack use: `docker ps`, `docker images`, `docker compose config`, `docker compose up -d --wait`, `docker compose down`, `docker compose logs`
- Reading and editing files inside the workspace, and creating new files the task requires
- `git add` and `git commit` for work the user has already asked for
- `git push` to a feature branch, after the mandatory secret scan in `secret-safety.md` passes

## Still confirm first

Only these need explicit approval, because they are hard to reverse or affect shared state:

- `git push` to `main`, any force-push, `--amend` on a pushed commit, `reset --hard`, `rebase`, `filter-branch`, or any history rewrite
- Merging a pull request
- Deleting or moving any file or directory, and `git rm`, `git clean`, `rm -rf`
- Creating or deleting a git tag
- Changing GitHub repository settings, branch protection, or visibility
- Anything touching a production environment or shared infrastructure
- Installing system-wide software or changing global tool configuration

## Behaviour

- Prefer acting over asking. When a routine step is needed to finish the task, take it and report the result afterwards.
- Do not ask permission for something already listed as pre-approved above.
- Ask one consolidated question when a real decision is genuinely needed, rather than several small ones.
- When a secret scan blocks a push, that block stands. Report it and wait — pre-approval does not override `secret-safety.md`.

## Precedence

- These rules apply in every session.
- `secret-safety.md` wins on any conflict about pushing or credentials.
- The file-preservation rules above override any instruction to clean up, simplify, or reduce the repository.
