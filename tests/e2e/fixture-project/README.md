# End-to-end fixture project

The Node.js service the criterion-10 journey (design.md §12.6) operates on. §8.3.2 requires the
journey to run the real agent binary "with a fixture Node.js project mounted"; this is that project.

It is deliberately **not** production-shaped:

- no `Dockerfile` — step 6 generates one, so shipping one would make that step assert nothing
- no `.github/workflows` — the readiness score would otherwise not have room to move
- no test suite — same reason
- no dependencies — `npm ci` would need a lockfile and a registry round trip in CI, and the journey
  is about ForgeOps generating artifacts for this service, not about the service running

So its readiness score is genuinely low, which is what makes step 5's assertion meaningful rather
than a tautology against a project that was already complete.
