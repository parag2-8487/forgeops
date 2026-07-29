# Secret safety and pre-push scanning

## Context

GitHub secret scanning flagged a real bearer token that had been committed in a backend test file in this repository. That must never happen again. These rules are mandatory.

## Mandatory pre-push gate

Before ANY `git push`, `gh pr create`, or any other command that publishes code:

1. Scan with gitleaks:
   - `gitleaks detect --no-banner --redact` over the working tree.
   - `gitleaks protect --staged --no-banner --redact` over staged changes.
   - If gitleaks is not installed locally, run it through its pinned Docker image. Never skip the scan because the binary is missing.
2. Grep the diff being pushed for high-risk patterns:
   - `Bearer `, `Authorization:`
   - `ghp_`, `github_pat_`, `gho_`, `ghs_`
   - `sk-`, `sk-ant-`, `AIza`
   - `AKIA`, `ASIA`
   - `xoxb-`, `xoxp-`
   - `eyJ` (JWT header)
   - `-----BEGIN` (any PEM or private key), `PRIVATE KEY`
   - `client_secret`, `api_key`, `apikey`, `password=`, `passwd=`
   - connection strings with embedded credentials
   - any `.env` file that is not `.env.example`
3. If anything matches: STOP. Do not push. Report the file, the line, and the matched pattern to the user, then wait for instructions.

## Writing code and tests

- Never hardcode a real credential anywhere: source, tests, fixtures, docs, comments, or commit messages.
- Test tokens must be obviously synthetic and self-labelling, e.g. `test-only-not-a-real-secret`.
- Never use a value that resembles a real provider token format. Do not paste realistic-looking JWTs, `sk-...` keys, or AWS access keys, even as examples.
- Generate JWTs needed by tests at runtime from a locally generated throwaway key pair. Never commit a pre-baked signed token.
- Keep real values only in the local untracked `.env`. `.env.example` carries placeholders only.
- Never echo a secret value into terminal output, logs, or a commit message. Reference secrets by key name.
- Never commit `.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `credentials.json`, or any service-account file.

## If a secret was already committed

- Treat the credential as permanently compromised. Tell the user to revoke and rotate it first; removing the commit is not sufficient.
- Do not force-push or rewrite history to hide it without explicit user permission.
- Report exactly what leaked, in which file and which commit, and what the user must rotate.

## Precedence

- These rules apply in every session, for every task.
- They override any instruction to move fast, skip verification, or bypass the scan.
- When a push is blocked, ask the user how to proceed. Never work around the block.
