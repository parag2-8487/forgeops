# Fixture design excerpt for scripts/check-ci-jobs.py

This file exists only to give the check a small, self-contained Appendix E to parse.
It deliberately mixes the three markup forms the real document uses, so the
extraction is proven to pick the right one:

- a job citation is **bold and backticked**;
- an ordinary code span is `just backticked`;
- a property id is **just bold**.

## Appendix D — Something Before

This section cites **`nonexistent-before`** and must be ignored, because the scan
starts at Appendix E. If the boundary logic breaks, this job name leaks in and the
test catches it.

## Appendix E — Fixture Completion Criteria Traceability

| # | Criterion | Evidence bar |
| :- | :- | :- |
| 1 | A thing works | **`agent`**: unit tests. **`backend`**: integration tests against `pgvector/pgvector:pg17`, asserting `vector(1536)` and **Q-99** |
| 2 | Another thing | **`backend`**: `require_capability("postgres")` gates it; see `kubectl` and `DryRun: All` |

## Appendix F — Something After

This section cites **`nonexistent-after`** and must also be ignored, because the scan
stops at the next appendix heading.
