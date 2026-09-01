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
          // NO host-resolver rule, deliberately. An earlier version mapped an invented hostname here
          // so the browser and the backend could share one IdP URL. It made the journey pass while
          // breaking the application for every real browser, which is the worst possible trade: the
          // test was measuring a topology only the test had. The split now lives in
          // OIDC_PUBLIC_BASE_URL, so this browser is configured exactly like a person's.
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
        {
          // Criterion 13's browser half, kept OUT of the journey deliberately. The journey is serial
          // and stateful, so adding a step to it would make "13/13" mean something different every
          // time one was added — and this assertion needs a generation run of its own, because step 6
          // posts through `page.request` and never touches the wizard at all.
          //
          // Its own project rather than a member of `journey` so it can carry its own timeout: it
          // makes one real model call, which the journey's per-step 180s bound would cut off.
          name: "paint",
          testMatch: /sse-paint\.spec\.ts/,
          use: { ...devices["Desktop Chrome"] },
          timeout: 2_460_000,
        },
        {
          // Part E's browser walk: nothing to an applied change set, by CLICKING.
          //
          // Its own project for the same two reasons `paint` is one. It is stateful — it pairs a device
          // and applies a change set — so folding it into `journey` would change what "13/13" counts,
          // and it makes a real generation call, so it needs the same generous per-test bound rather
          // than the journey's 180s.
          //
          // Distinct from the journey in KIND, not only in position: the journey drives most of its
          // steps through `page.request`, which is why it stayed green while the browser could not
          // reach most of the engine. This one asserts the half that was missing.
          name: "onboarding",
          testMatch: /onboarding\.spec\.ts/,
          use: { ...devices["Desktop Chrome"] },
          timeout: 2_460_000,
        },
        {
          // Part F: the printed commands, executed verbatim, with the agent as a HOST process.
          //
          // Its own project because it is the only spec that runs the agent OUTSIDE a container. Every
          // other one calls `composeExec("agent", [...])` with arguments the spec chose — which proves
          // the CLI works and proves nothing about the command the UI printed. The UI printed
          // `forgeops-agent pair --code X`, which fails three ways, and nothing noticed.
          //
          // A generous bound: it builds the agent, installs it onto PATH, pairs, and indexes a real
          // workspace, all as separate processes on the runner.
          name: "printed-instructions",
          testMatch: /printed-instructions\.spec\.ts/,
          use: { ...devices["Desktop Chrome"] },
          timeout: 900_000,
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
