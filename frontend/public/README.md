Static assets served at the site root by Next.js.

Phase 0 ships no static assets, but the directory is tracked because
`frontend/Dockerfile` copies `/app/public` into the runtime stage. Without it the
image build fails at that COPY step.
