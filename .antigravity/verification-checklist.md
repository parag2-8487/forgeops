# Per-Leaf Verification Checklist

Before marking ANY leaf as complete, you MUST verify:

1. Static Checks:
   - Linter passes (ruff / golangci-lint)
   - Code formatter passes (ruff format / gofmt)
   - Import smoke test passes
   - Secret scan passes (gitleaks)

2. Test Evidence:
   - Leaf's own unit tests pass
   - Integration tests pass (if applicable)
   - Property tests pass (if applicable)

3. Record-Keeping:
   - LEARNING-JOURNAL.md updated (what/why/rejected/cost)
   - PROGRESS.md updated with evidence column filled
   - .antigravity/session-state.json updated
   - Commit with leaf number in message
