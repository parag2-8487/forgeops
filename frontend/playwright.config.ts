import { defineConfig, devices } from "@playwright/test";

/**
 * Two projects, because the two specs need opposite things.
 *
 * `smoke` runs `shell.spec.ts` against a frontend Playwright starts itself. It is fast, needs no
 * backend, and is what most changes should be gated on.
 *
 * `journey` runs the criterion-10 journey (§12.6) against a stack that is ALREADY UP — the compose
 * overlay, with a real backend, a real database and a real paired agent. It cannot use `webServer`:
 * the frontend is a container in that stack, and Playwright starting a second one on the same port
 * would either fail to bind or serve a build talking to nothing.
 *
 * Hence `FORGEOPS_E2E_EXTERNAL_STACK`. When set, no server is started and the journey is included.
 * When unset, only the smoke test runs — so `pnpm test:e2e` on a laptop does not silently attempt a
 * 13-step journey against services that are not running and report thirteen confusing failures.
 */
const EXTERNAL_STACK = Boolean(process.env.FORGEOPS_E2E_EXTERNAL_STACK);

export default defineConfig({
  testDir: "./e2e",
  // The journey is serial within its own describe; the smoke test is independent.
  fullyParallel: !EXTERNAL_STACK,
  forbidOnly: !!process.env.CI,
  // NO RETRIES FOR THE JOURNEY. It mutates shared state — it pairs a device, applies a change set
  // and reverts it — so a retry would re-run step 3 against a consumed pairing code and step 13
  // against an already-reverted change set. Retrying a stateful journey converts one real failure
  // into a different, misleading one.
  retries: EXTERNAL_STACK ? 0 : process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? [["html"], ["list"]] : "html",
  use: {
    baseURL: process.env.E2E_FRONTEND_URL ?? "http://localhost:3000",
    // `retain-on-failure` rather than `on-first-retry`: with retries disabled for the journey,
    // `on-first-retry` would never produce a trace, and §8.3.2 requires traces on failure because
    // "an e2e failure with no artifacts is a rerun rather than a diagnosis".
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: EXTERNAL_STACK ? "retain-on-failure" : "off",
  },
  projects: EXTERNAL_STACK
    ? [
        {
          name: "journey",
          testMatch: /journey\.spec\.ts/,
          use: { ...devices["Desktop Chrome"] },
          // The journey drives a real IdP redirect and a real agent apply. Individual steps set
          // their own waits; this is the outer bound per step.
          timeout: 180_000,
        },
        {
          name: "smoke",
          testMatch: /shell\.spec\.ts/,
          use: { ...devices["Desktop Chrome"] },
        },
      ]
    : [
        {
          name: "smoke",
          testMatch: /shell\.spec\.ts/,
          use: { ...devices["Desktop Chrome"] },
        },
      ],
  webServer: EXTERNAL_STACK
    ? undefined
    : {
        command: process.env.CI ? "pnpm build && pnpm start" : "pnpm dev",
        url: "http://localhost:3000",
        reuseExistingServer: !process.env.CI,
      },
});
