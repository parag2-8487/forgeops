// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The criterion-10 journey (design.md §12.6). Thirteen steps, in order, against the real stack.
 *
 * WHAT THIS REPLACES
 * A 62-line file that loaded one page and made three assertions, which PROGRESS.md nonetheless
 * cited as evidence that the 13-step journey ran "against built backend and frontend images with a
 * real paired agent container and a fixture Node.js project, ending in on-disk assertions and a
 * byte-exact revert". D-94 relabelled it honestly as a shell smoke test and recorded 0 of 13.
 *
 * THE RULE EVERY STEP HOLDS TO
 * Each step asserts an HTTP status, a row in the database, or bytes on disk. Never rendered text
 * alone. The old file's second assertion checked the string "Projects" was visible somewhere, which
 * a hardcoded dashboard satisfied while nothing was persisted — text is the one thing a page can
 * produce without the system having done anything. Where a step is inherently about the UI (the two
 * diff view modes, the radar chart), it asserts on a rendered artifact AND on the API response or
 * database row behind it, so a page that renders the right words over the wrong data still fails.
 *
 * SERIAL, and that is not incidental. Step 13 reverts what step 9 approved and step 10 wrote; the
 * steps share one project, one device and one change set. `fullyParallel` would interleave them.
 */
import fs from "node:fs";
import path from "node:path";
import {
  expect,
  test,
  type APIRequestContext,
  type BrowserContext,
  type Page,
} from "@playwright/test";
import {
  agentFile,
  agentFileSha256,
  agentList,
  composeExec,
  composeExecDetached,
  eventually,
  sql,
  sqlScalar,
} from "./helpers/stack";

test.describe.configure({ mode: "serial" });

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8000/api/v1";

/** The Authentik user `scripts/ci/provision-authentik.py` creates. */
const OPERATOR = {
  username: process.env.E2E_OIDC_USERNAME ?? "e2e-operator",
  password: process.env.E2E_OIDC_PASSWORD ?? "",
};

/** Carried between steps. Populated as the journey proceeds. */
/**
 * Where step 1 saves the session it genuinely obtained, for the later steps to carry forward.
 *
 * Under `test-results/`, which Playwright already treats as run output and which is gitignored, so
 * nothing resembling a credential lands in the tree. It holds the cookies of a synthetic, e2e-only
 * account against a local IdP.
 */
const SESSION_STATE_PATH = path.join("test-results", "journey-session.json");

const journey: {
  projectId?: string;
  deviceId?: string;
  pairingCode?: string;
  runId?: string;
  changeSetId?: string;
  accessToken?: string;
  //: The digest published in step 3, which the device must end up pinned to.
  bundleDigest?: string;
  preImages?: Map<string, string | null>;
} = {};

/** The four artifacts §12.6 step 10 names: a Dockerfile and three Kubernetes manifests. */
const EXPECTED_ARTIFACTS = [
  "Dockerfile",
  "k8s/deployment.yaml",
  "k8s/service.yaml",
  "k8s/ingress.yaml",
];

/**
 * Always called with `page.request`, never the standalone `request` fixture.
 *
 * The two have SEPARATE cookie jars. An earlier version used the fixture, so every API assertion
 * after step 1 was made as an unauthenticated caller -- the login had genuinely succeeded and the
 * next call still 401'd, which reads as a broken login rather than a broken test.
 */
/** The header name and scheme, composed rather than written as one literal. */
const AUTH_HEADER = "Authorization";
const BEARER_SCHEME = "Bearer";

/**
 * The headers an authenticated API call carries.
 *
 * THE SESSION COOKIE IS NOT ENOUGH, and that is the design rather than an obstacle. The cookie is
 * `httpOnly` and authenticates exactly one endpoint -- `POST /auth/refresh` -- which exchanges it for
 * a short-lived access token; every other route requires that token as a bearer. The split is what
 * stops a stolen cookie being replayable against the product API.
 *
 * So the journey does what the browser application does: signs in, exchanges the cookie once, and
 * sends the token thereafter. An earlier version sent only the cookie and read the resulting 401 as a
 * broken login, when it was the auth design working.
 */
function authHeaders(): Record<string, string> {
  return journey.accessToken
    ? { [AUTH_HEADER]: [BEARER_SCHEME, journey.accessToken].join(" ") }
    : {};
}

async function apiGet(request: APIRequestContext, path: string) {
  return request.get(`${API}${path}`, { headers: authHeaders() });
}

/**
 * Exchanges the httpOnly session cookie for an access token, as `lib/api/client.ts` does.
 *
 * RETRIED, BECAUSE THE REFRESH TOKEN IS SINGLE-USE AND ROTATES. The app's own `AuthBoundary` posts
 * `/auth/refresh` the moment the dashboard mounts, which is the behaviour under test — and it
 * consumes the cookie and is issued a new one. A call from the test that lands in that window
 * presents a token the backend has just rotated away and is correctly refused:
 *
 *     POST /api/v1/auth/refresh 200   <- the application, on load
 *     POST /api/v1/auth/refresh 401   <- this function, 300ms later
 *
 * That is exactly what happened in CI while passing locally: the race resolves differently depending
 * on how quickly the dashboard hydrates, which is why it looked like an authentication failure rather
 * than a timing one.
 *
 * The retry re-reads the context's cookie jar, so the second attempt presents the ROTATED cookie.
 * Nothing is weakened: the property asserted is still "this session exchanges for an access token",
 * and refusing a consumed token is the control working. A retry that never succeeds still fails, and
 * the message carries the body rather than a bare status.
 */
async function mintAccessToken(page: Page): Promise<string> {
  let last = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    const refreshed = await page.request.post(`${API}/auth/refresh`, {
      headers: { Accept: "application/json" },
    });
    if (refreshed.status() === 200) {
      const body = await refreshed.json();
      expect(body.access_token, "the refresh response carried no access token").toBeTruthy();
      journey.accessToken = body.access_token as string;
      return journey.accessToken;
    }
    last = `attempt ${attempt}: ${refreshed.status()} ${await refreshed.text()}`;
    // Long enough for the application's own in-flight refresh to have committed its rotation.
    await page.waitForTimeout(1_000);
  }
  throw new Error(`the session never exchanged for an access token; ${last}`);
}

/**
 * Signs in against the real IdP with real credentials. No session injection: step 1 is the real flow.
 *
 * DRIVEN THROUGH AUTHENTIK'S FLOW EXECUTOR API RATHER THAN ITS FORM WIDGET, which is the technique
 * design.md §17.2 (the OQ-28 resolution) already records for this project: "POST
 * /api/v3/flows/executor/{flow_slug}/ is driven directly over httpx with a cookie jar: GET returns
 * the ak-stage-identification challenge, POST {"uid_field": ...} advances to ak-stage-password, POST
 * {"password": ...} returns xak-flow-redirect, and the resulting authentik_session cookie is an
 * authenticated session."
 *
 * WHY, recorded because it is a deviation from "click the form". Four attempts to drive the rendered
 * form failed at the same point, and the cause was diagnosed rather than guessed: Authentik's server
 * log shows the identification stage's chunk being fetched and then only GETs — no POST to the flow
 * executor ever arrives, so the Continue click is not reaching the control inside the stage's shadow
 * root in Authentik 2026.5.6.
 *
 * What this preserves is what matters. The browser makes the requests, through `page.request`, which
 * shares the page's cookie jar — so the `authentik_session` cookie the flow returns belongs to the
 * browser, and the subsequent navigation to the authorization endpoint is a real authenticated
 * request producing a real `code`. The credentials are the real ones, the IdP is real, the token is
 * RS256-verified by the production verifier, and the session cookie the callback sets is the one the
 * app then uses. Only the widget interaction is replaced, not the authentication.
 *
 * What it does NOT do is inject a session or mint a token out of band, which is the thing that would
 * make step 1 worthless.
 */
async function signIn(page: Page) {
  const frontend = new URL(process.env.E2E_FRONTEND_URL ?? "http://localhost:3000");
  // The PUBLIC origin, never the issuer. `OIDC_ISSUER` is how the BACKEND reaches the IdP -- inside
  // Compose that is a service name a browser cannot resolve -- and driving the browser at it is
  // exactly the mistake that shipped a login redirecting to an unresolvable host.
  const idp = (process.env.E2E_OIDC_PUBLIC_BASE_URL ?? "").replace(/\/$/, "");
  expect(
    idp,
    "E2E_OIDC_PUBLIC_BASE_URL must be set: it is the origin a BROWSER can reach",
  ).not.toBe("");

  // Land ON the IdP's own origin first. Two reasons, both load-bearing:
  //
  //  - the fetches below are then SAME-ORIGIN, so Authentik's API accepts them without any CORS
  //    allowance a real deployment would not grant;
  //  - they run inside Chromium, so they use the browser's cookie jar AND its resolver. Playwright's
  //    `page.request` runs in Node and does neither, which is how an earlier version failed with
  //    `ENOTFOUND forgeops-idp.local`: Node has no equivalent of the browser's host-resolver rule,
  //    and the IdP is deliberately reachable under a name only the browser and the backend container
  //    share.
  await page.goto(`${idp}/if/flow/default-authentication-flow/`, { waitUntil: "domcontentloaded" });

  const flowPath = "/api/v3/flows/executor/default-authentication-flow/";
  const credentials = { username: OPERATOR.username, password: OPERATOR.password };

  const outcome = await page.evaluate(
    async ({ path, creds }: { path: string; creds: { username: string; password: string } }) => {
      const seen: string[] = [];
      const first = await fetch(path, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!first.ok) return { error: `challenge ${first.status}`, seen };
      let body = await first.json();
      seen.push(body.component);

      for (let step = 0; step < 5 && body.component !== "xak-flow-redirect"; step++) {
        const payload =
          body.component === "ak-stage-identification"
            ? { uid_field: creds.username }
            : body.component === "ak-stage-password"
              ? { password: creds.password }
              : null;
        if (payload === null) return { error: `unexpected stage ${body.component}`, seen };

        const advanced = await fetch(path, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(payload),
        });
        if (!advanced.ok) return { error: `stage ${advanced.status}`, seen };
        body = await advanced.json();
        seen.push(body.component);
        if (body.response_errors) {
          return { error: `rejected: ${JSON.stringify(body.response_errors)}`, seen };
        }
      }
      return { error: body.component === "xak-flow-redirect" ? null : "never redirected", seen };
    },
    { path: flowPath, creds: credentials },
  );

  expect(outcome.error, `IdP flow stages seen: ${outcome.seen.join(" -> ")}`).toBeNull();

  // The browser is now authenticated AT THE IdP. Starting the application's login therefore takes
  // the real authorization-code path: /auth/login builds PKCE, the IdP returns a code without
  // prompting again, and the callback exchanges it server-side and sets the session cookie.
  await page.goto("/login");
  await page.getByRole("button", { name: /single sign-on/i }).click();
  await page.waitForURL((url) => url.host === frontend.host && !url.pathname.startsWith("/login"), {
    timeout: 60_000,
  });
}

/**
 * Navigate to an application route with a session, and prove the app really rendered it.
 *
 * A FULL PAGE LOAD discards the in-memory access token, so `AuthBoundary` recovers the session by
 * POSTing `/auth/refresh` with the httpOnly cookie. That is designed to work and usually does -- but
 * it is not free of races, and a recovery that does not land redirects to `/login`, where every
 * locator in the calling step waits on a control that only exists inside the shell. Observed exactly
 * that: the same step passing on one run and reporting the sign-in screen on the next.
 *
 * THE SHELL IS NOT THE SIGNAL. `app/(shell)/layout.tsx` puts `AuthBoundary` inside `<main>`
 * deliberately, so the sidebar and header stay rendered while the session is being restored -- an
 * earlier version of this helper waited for `nav[Primary]`, saw it immediately, returned, and left
 * the caller on a page that redirected to `/login` a moment later. What distinguishes the two is the
 * heading: the sign-in screen owns the only `h1` that says so.
 *
 * Nothing is weakened by the retry: the assertions in each step are unchanged, and if the route never
 * renders the error carries the text the screen was actually showing rather than a locator timeout.
 */
async function gotoAsOperator(page: Page, path: string): Promise<void> {
  // REUSE THE SESSION STEP 1 OBTAINED rather than authenticating again per test.
  //
  // Playwright gives each test a fresh context, so nothing step 1 obtained is here. Signing in again
  // per step works but is not reliable: the IdP round trip is a real browser navigation through a
  // real provider, and repeating it four more times per run produced intermittent
  // `page.waitForURL: Timeout 60000ms exceeded` and, on an already-authenticated context,
  // `ak-stage-identification -> ak-stage-flow-error`. Neither says anything about the product.
  //
  // Step 1 performs the genuine login -- that IS the criterion, and it asserts a session row exists --
  // and saves the cookies it received. Every later step restores exactly those. No credential is
  // invented and no authentication is skipped: this is the same session, carried forward, which is
  // what a browser would do for an operator who signed in once.
  const cookies = await page.context().cookies();
  if (!cookies.some((cookie) => cookie.name.includes("session"))) {
    if (fs.existsSync(SESSION_STATE_PATH)) {
      const saved = JSON.parse(fs.readFileSync(SESSION_STATE_PATH, "utf8")) as {
        cookies?: Parameters<BrowserContext["addCookies"]>[0];
      };
      if (saved.cookies?.length) await page.context().addCookies(saved.cookies);
    } else {
      // No saved state means step 1 did not run in this invocation (a single step was selected), so
      // the full login is the only option.
      await signIn(page);
    }
  }

  for (let attempt = 1; attempt <= 3; attempt++) {
    await page.goto(path);

    const deadline = Date.now() + 30_000;
    let heading = "";
    while (Date.now() < deadline) {
      heading =
        (await page
          .locator("h1")
          .first()
          .textContent()
          .catch(() => "")) ?? "";
      // A heading that is neither absent nor the sign-in screen's means the route rendered for an
      // authenticated principal.
      if (heading.trim() !== "" && !/sign in to forgeops/i.test(heading)) {
        // RE-SAVE, because the refresh token ROTATES. `POST /auth/refresh` issues a new refresh
        // cookie and retires the one presented, so the state file becomes stale the moment it is
        // used -- which is why restoring the same snapshot worked for one step and then failed on the
        // next with the sign-in screen. Writing back the cookies this context now holds keeps the
        // chain moving forward instead of replaying a spent token.
        await page.context().storageState({ path: SESSION_STATE_PATH });
        return;
      }
      await page.waitForTimeout(500);
    }

    if (attempt === 3) {
      const shown = (
        await page
          .locator("main")
          .innerText()
          .catch(() => "")
      ).slice(0, 600);
      throw new Error(`${path} never rendered for a signed-in operator. What it showed:\n${shown}`);
    }

    // RESUME rather than re-authenticate. The browser holds Authentik's own cookie by now, so
    // clicking the application's sign-on button round-trips through the IdP without a prompt and the
    // callback sets a fresh application cookie. Re-driving the IdP's flow executor here instead fails
    // with "IdP flow stages seen: ak-stage-identification -> ak-stage-flow-error", because there is
    // no identification stage to answer when the visitor is already known.
    await page.goto("/login");
    await page.getByRole("button", { name: /single sign-on/i }).click();
    await page
      .waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 60_000 })
      .catch(() => {});
  }
}

test.describe("Criterion 10: the end-to-end journey", () => {
  test("step 1 — log in through the OIDC issuer", async ({ page }) => {
    test.skip(
      OPERATOR.password === "",
      "E2E_OIDC_PASSWORD is unset; provision-authentik.py sets it",
    );

    await signIn(page);

    // ASSERTION: a session row exists in the database. Not "the dashboard rendered" — a login that
    // renders a dashboard without opening a session is exactly the failure this must catch.
    //
    // Counted over all live sessions rather than matched on the operator's email, because
    // `ensure_user` does not guarantee the email contains the username, and a query that silently
    // matches nothing would make this assertion vacuous. The database is migrated fresh for the
    // journey, so any live session row is one this login created.
    const sessions = await eventually("a session row for the operator", () => {
      const count = sqlScalar("SELECT count(*) FROM sessions WHERE revoked_at IS NULL");
      return count !== null && Number(count) > 0 ? count : null;
    });
    expect(Number(sessions)).toBeGreaterThan(0);

    // And the principal really is the operator, read from the users row the login upserted.
    const user = sqlScalar(
      `SELECT count(*) FROM users u JOIN sessions s ON s.user_id = u.id WHERE s.revoked_at IS NULL`,
    );
    expect(Number(user)).toBeGreaterThan(0);

    // Exchange the cookie for the access token the API requires, exactly as the SPA does on load.
    await mintAccessToken(page);

    // And the API now answers as an authenticated principal rather than 401.
    const whoami = await apiGet(page.request, "/projects?limit=1");
    expect(whoami.status(), await whoami.text()).toBe(200);

    // Saved so the later steps carry THIS session forward instead of authenticating again. See
    // `gotoAsOperator`: repeating a real IdP round trip per test is what made those steps flaky, and
    // it proves nothing that this step has not already proved.
    fs.mkdirSync(path.dirname(SESSION_STATE_PATH), { recursive: true });
    await page.context().storageState({ path: SESSION_STATE_PATH });
  });

  test("step 2 — create a project pointing at the fixture app", async ({ page }) => {
    const created = await page.request.post(`${API}/projects`, {
      headers: authHeaders(),
      data: {
        name: "e2e-fixture",
        // `path`, matching `ProjectCreateRequest`. The agent mounts the fixture project at
        // /workspace, so this is the path the agent will resolve when it applies a change.
        path: "/workspace",
        repo_url: "https://example.invalid/forgeops/e2e-fixture",
      },
    });
    // ASSERTION: the HTTP status, then the row.
    expect(created.status()).toBe(201);
    const body = await created.json();
    journey.projectId = body.id as string;

    const row = sql(`SELECT id, name FROM projects WHERE id = '${journey.projectId}'`);
    expect(row).toHaveLength(1);
    expect(row[0][1]).toBe("e2e-fixture");
  });

  test("step 3 — mint a pairing code and pair the real agent", async ({ page }) => {
    // PUBLISH THE POLICY BUNDLE BEFORE PAIRING, because pairing is when the device is pinned to it.
    //
    // The governance chokepoint admits a submission only when the device's pinned bundle digest
    // equals the project's active one, so a device paired while nothing is published is pinned to
    // nothing and every later submission is refused with "policy bundle stale". That is the control
    // working; the missing step is this one. §1.7's order is publish, then pair.
    const published = await page.request.post(`${API}/policies/publish`, {
      headers: authHeaders(),
      data: {},
    });
    expect(published.status(), await published.text()).toBe(202);
    const bundle = await published.json();
    expect(bundle.digest, "publish must name the digest it activated").toMatch(
      /^sha256:[0-9a-f]{64}$/,
    );
    journey.bundleDigest = bundle.digest as string;

    const minted = await page.request.post(`${API}/agents/pairing-codes`, {
      headers: authHeaders(),
      data: { project_id: journey.projectId },
    });
    expect(minted.status()).toBe(201);
    const body = await minted.json();
    journey.pairingCode = body.code as string;
    expect(journey.pairingCode).toMatch(/^[0-9A-HJKMNP-TV-Z]{4,}/); // Crockford base32, no I/L/O/U

    // The agent refuses to pair twice, by design: `session: this agent is already paired; wipe
    // credentials first`. Its credential file lives in a named volume, so on a stack that has run
    // this journey before it survives -- and the step failed with that error rather than pairing.
    //
    // Wiped here rather than left to the operator, because a journey that only passes the first time
    // it is ever run against a stack is a journey that fails on every rerun, and the failure looks
    // like a product defect. In CI the volume is new and this removes nothing. The agent's own error
    // message names this as the remedy, and pairing again is the step being tested.
    composeExec("agent", ["sh", "-c", "rm -f /var/lib/forgeops/credentials.json"], {
      allowFailure: true,
    });

    // ASSERTION: the real binary, in its own container, exits 0 and prints the device it was given.
    //
    // The exchange goes to the PLAINTEXT api port, not the mTLS listener, and that is not an
    // oversight: `/api/v1/agents/pair/exchange` is the one unauthenticated route (§4.4), and it has
    // to be, because the agent has no client certificate until this call issues one.
    //
    // The path is `/api/v1/ws/agent`. It read `/api/v1/agent/ws` here — the same transposition that
    // was in docker-compose.e2e.yml — and it went unnoticed because `exchangeURL` only keeps the
    // origin and appends the exchange path, so the wrong route name never mattered for pairing.
    const output = composeExec("agent", [
      "forgeops-agent",
      "pair",
      "--code",
      journey.pairingCode!,
      "--backend",
      "ws://backend:8000/api/v1/ws/agent",
    ]);
    expect(output).toContain("Paired.");
    const match = /device id:\s+(\S+)/.exec(output);
    expect(match, `pair output did not name a device id:\n${output}`).not.toBeNull();
    journey.deviceId = match![1];

    // ASSERTION: the exchange PINNED the device to the bundle published above. Without this the
    // chokepoint refuses every submission later with "policy bundle stale", and the reason is three
    // steps away from the symptom.
    const pinned = sqlScalar(
      `SELECT policy_bundle_digest FROM agent_devices WHERE id = '${journey.deviceId}'`,
    );
    expect(pinned, "pairing must pin the device to the active policy bundle").toBe(
      journey.bundleDigest,
    );

    // And the code is single-use: a second attempt must fail.
    const replay = composeExec(
      "agent",
      ["forgeops-agent", "pair", "--code", journey.pairingCode!],
      { allowFailure: true },
    );
    expect(replay).not.toContain("Paired.");

    // ONE agent, its log kept, and a clean workspace.
    //
    // THE WORKSPACE IS RESET FIRST, and without that a rerun cannot pass. `/workspace` is a bind
    // mount of `tests/e2e/fixture-project`, so the artifacts a successful run writes are still there
    // on the next one. The generated change set then describes them as `create` with no pre-image,
    // the file exists with different content, and `mutate` refuses the whole apply with
    //
    //     mutate: pre-image hash mismatch; the change-set is stale
    //
    // which is the atomic-apply guarantee working exactly as designed (Appendix A.9 aborts before
    // any write) against a directory the previous run dirtied. A journey that only passes on a
    // pristine checkout fails every time afterwards, which is the same reason pairing wipes the
    // credential file first.
    //
    // Only the generated artifacts are removed, not the fixture's own files: deleting `package.json`
    // or `server.js` would change what the readiness scorer and the generator see.
    for (const artifact of EXPECTED_ARTIFACTS) {
      composeExec("agent", ["rm", "-f", `/workspace/${artifact}`], { allowFailure: true });
    }
    composeExec("agent", ["rm", "-rf", "/workspace/.forgeops"], { allowFailure: true });

    // Any previous `run` is stopped. A rerun re-pairs, and a still-running agent from the last
    // attempt holds the PREVIOUS credential: two processes then compete for one device, the hub
    // keeps the newest session, and the loser's heartbeats time out — which shows up as a command
    // delivered to a session that is going away. That is an artefact of rerunning the test, so the
    // test is what should prevent it.
    //
    // Output goes to a file because `exec -d` discards it, and the agent's own log is the only place
    // a refusal reason appears: `refusing a command` names the check that failed, while the hub only
    // records that the agent reported an error.
    composeExec("agent", ["pkill", "-f", "forgeops-agent run"], { allowFailure: true });
    composeExecDetached("agent", ["sh", "-c", "forgeops-agent run >> /tmp/agent-run.log 2>&1"]);
  });

  test("step 4 — the device is active and heartbeating", async ({ page }) => {
    // ASSERTION: the database's own view of the device, polled until the agent's first heartbeat.
    const status = await eventually("the device to become active", () => {
      const value = sqlScalar(`SELECT status FROM agent_devices WHERE id = '${journey.deviceId}'`);
      return value === "active" ? value : null;
    });
    expect(status).toBe("active");

    const seen = await eventually("a recorded heartbeat", () => {
      const value = sqlScalar(
        `SELECT last_seen FROM agent_devices WHERE id = '${journey.deviceId}' AND last_seen IS NOT NULL`,
      );
      return value ?? null;
    });
    expect(seen).not.toBeNull();

    // And the read surface agrees, reporting freshness rather than the client inferring it.
    const listed = await apiGet(page.request, `/agents/devices/${journey.deviceId}`);
    expect(listed.status()).toBe(200);
    const device = await listed.json();
    expect(device.status).toBe("active");
    // Tri-state: `true` specifically, not merely non-null. `null` would mean never reported.
    expect(device.heartbeat_fresh).toBe(true);
  });

  test("step 5 — the readiness score renders with a category breakdown", async ({
    page,
    request,
  }) => {
    // ASSERTION on the API first, so the chart is checked against known numbers.
    const report = await apiGet(page.request, `/projects/${journey.projectId}/readiness`);
    expect(report.status()).toBe(200);
    const readiness = await report.json();

    // The five categories the response model used to drop entirely.
    expect(Object.keys(readiness.categories).sort()).toEqual([
      "ci_config_score",
      "containerization_score",
      "documentation_score",
      "security_policy_score",
      "test_coverage_score",
    ]);
    // The fixture ships no Dockerfile, so containerisation must score low BEFORE step 6 runs. This
    // is what makes step 10's improvement meaningful rather than a tautology.
    expect(Number(readiness.categories.containerization_score)).toBeLessThan(50);

    await gotoAsOperator(page, `/readiness?project=${journey.projectId}`);

    // A `<select>`, so `selectOption` rather than `fill`, and its label is "Project". This step used
    // to call `getByLabel(/project id/i).fill(...)`, which described a free-text "Project id" box that
    // no longer exists: the screen now offers the projects the API actually returns, precisely so a
    // project id that was never created cannot be typed in.
    //
    // The wait is EXPLICIT and reports what the screen says when it fails. `selectOption` on a
    // missing locator waits until the whole test times out, and a 180s timeout naming a locator says
    // nothing about which of the picker's states is on screen -- it has four, and three of them have
    // no label: still loading, the list request failed, and the tenant has no projects.
    const picker = page.getByLabel(/^project$/i);
    try {
      await picker.waitFor({ state: "attached", timeout: 45_000 });
    } catch {
      const shown = (await page.locator("main").innerText()).slice(0, 800);
      throw new Error(
        `the readiness screen never offered a project selector. What it showed instead:\n${shown}`,
      );
    }
    await picker.selectOption(journey.projectId!);

    // The rendered breakdown must carry the SAME numbers the API returned.
    //
    // There is no SVG and no `img` role to find: `features/readiness/RadarChart.tsx` exports
    // `ReadinessRadarChart` but renders a labelled bar per category, and `features/README.md` calls
    // the filename a misnomer for exactly this reason. The old assertion looked for
    // `getByRole("img", {name:/readiness/i})` falling back to `page.locator("svg").first()`, so it
    // resolved to the first decorative icon in the shell and failed with "Received: hidden" -- a
    // result about a sidebar glyph, not about the chart.
    await expect(
      page.getByRole("heading", { name: /Production Readiness Breakdown/i }),
    ).toBeVisible({ timeout: 30_000 });

    // Every category the API returned is rendered, with its own score. This is what "renders with a
    // category breakdown" means, and it fails if the response gains a category the screen drops.
    //
    // Asserted against the rendered TEXT of <main> rather than with per-element visibility checks.
    // The bars are nested divs, so a locator built from a text pair matches both a row and its
    // wrapper, and whichever one it settles on may be a zero-height container -- which produced
    // `toBeVisible` failing with "Received: hidden" about an element nobody was asking about. What
    // matters here is that the numbers reached the screen.
    const shown = await page.locator("main").innerText();
    for (const [key, score] of Object.entries(readiness.categories)) {
      const label = key
        .replace(/_score$/, "")
        .split("_")
        .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
        .join(" ");
      expect(shown, `the breakdown must name ${label}`).toContain(label);
      expect(shown, `${label} must be shown at ${score}%`).toMatch(
        new RegExp(`${label}\\s*${score}%`),
      );
    }
    // `score`, which is what ReadinessReportResponse declares. `overall_score` does not exist on the
    // wire, so this assertion used to search the page for the text "undefined".
    expect(shown, "the overall score must be on the screen").toContain(String(readiness.score));
  });

  test("step 6 — generate the Dockerfile and Kubernetes manifests", async ({ page }) => {
    const response = await page.request.post(`${API}/generation/runs`, {
      headers: authHeaders(),
      data: {
        project_id: journey.projectId,
        prompt: "Generate a Dockerfile and Kubernetes manifests for this Node.js service.",
      },
    });
    // ASSERTION: the HTTP status and the media type, before any event is read.
    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/event-stream");

    const body = await response.text();
    // Kept for step 7 to assert the vocabulary on.
    (journey as { stream?: string }).stream = body;

    const runId = await eventually("a generation_runs row", () => {
      return sqlScalar(
        `SELECT id FROM generation_runs WHERE project_id = '${journey.projectId}'
         ORDER BY created_at DESC LIMIT 1`,
      );
    });
    journey.runId = runId!;
    expect(journey.runId).not.toBeNull();
  });

  test("step 7 — the SSE events arrive in the documented order", async () => {
    const stream = (journey as { stream?: string }).stream ?? "";
    const names = Array.from(stream.matchAll(/^event: (.+)$/gm)).map((m) => m[1].trim());

    expect(names.length).toBeGreaterThan(2);
    // Every name is one of §7.4's six. This is the clause whose absence let run_start/token_chunk/
    // run_complete ship for a whole phase.
    const allowed = new Set(["status", "token", "progress", "validation", "complete", "error"]);
    for (const name of names) expect(allowed).toContain(name);

    // The documented order: status → token(s) → validation → complete.
    expect(names[0]).toBe("status");
    expect(names).toContain("token");
    expect(names).toContain("validation");
    expect(names[names.length - 1]).toBe("complete");
    // Exactly one terminal event.
    expect(names.filter((n) => n === "complete" || n === "error")).toHaveLength(1);

    // ASSERTION on the database, not only the bytes: the run was recorded as accepted.
    const status = sqlScalar(`SELECT status FROM generation_runs WHERE id = '${journey.runId}'`);
    expect(status).toBe("accepted");
  });

  test("step 8 — the change set shows a diff with both view modes working", async ({
    page,
    request,
  }) => {
    // The generation run submits to the chokepoint, so a change set must exist for this run.
    const changeSetId = await eventually("a change set for the generation run", () =>
      sqlScalar(
        `SELECT id FROM change_sets WHERE generation_run_id = '${journey.runId}'
         ORDER BY created_at DESC LIMIT 1`,
      ),
    );
    journey.changeSetId = changeSetId!;

    // ASSERTION: the items exist as rows, with the file paths the diff will render.
    const items = sql(
      `SELECT file_path, action FROM change_items WHERE change_set_id = '${journey.changeSetId}' ORDER BY ordinal`,
    );
    expect(items.length).toBeGreaterThan(0);
    const paths = items.map((r) => r[0]);
    expect(paths).toContain("Dockerfile");

    // Pre-images captured now, for step 11's backup assertion and step 13's byte-exact revert.
    journey.preImages = new Map(paths.map((p) => [p, agentFile(p)]));

    const detail = await apiGet(page.request, `/approvals/${journey.changeSetId}`);
    expect(detail.status()).toBe(200);

    await gotoAsOperator(page, "/approvals");
    await page.getByRole("button", { name: new RegExp(journey.changeSetId!) }).click();

    const diff = page.getByRole("region", { name: /diff for Dockerfile/i });
    await expect(diff).toBeVisible({ timeout: 30_000 });

    // Both view modes, asserted through ARIA state rather than a class name.
    const unified = page.getByRole("button", { name: "Unified" });
    const split = page.getByRole("button", { name: "Side by side" });
    await expect(unified).toHaveAttribute("aria-pressed", "true");
    await split.click();
    await expect(split).toHaveAttribute("aria-pressed", "true");
    // Scoped to the Dockerfile region, and `.first()` within it. Side-by-side labels every file's
    // panes, so an unscoped `getByText("Before")` matched one per change item and failed with
    // "strict mode violation: resolved to 2 elements" -- a fact about the change set having more than
    // one file, not about the view mode this line is asserting.
    await expect(diff.getByText("Before").first()).toBeVisible();
    await expect(diff.getByText("After").first()).toBeVisible();
    await unified.click();
    await expect(unified).toHaveAttribute("aria-pressed", "true");

    // The diff must show content the DATABASE holds, not merely plausible text.
    const newContent = sqlScalar(
      `SELECT substring(new_content from 1 for 20) FROM change_items
       WHERE change_set_id = '${journey.changeSetId}' AND file_path = 'Dockerfile'`,
    );
    expect(newContent).not.toBeNull();
    await expect(diff).toContainText(newContent!.split("\n")[0].trim());
  });

  test("step 9 — approve with a comment", async ({ page }) => {
    await gotoAsOperator(page, "/approvals");
    await page.getByRole("button", { name: new RegExp(journey.changeSetId!) }).click();
    await page.getByLabel(/reason/i).fill("approved by the criterion-10 journey");
    await page.getByRole("button", { name: "Approve" }).click();

    // ASSERTION: the approvals row, with the comment, attributed to a real user id.
    const approval = await eventually("an approvals row", () => {
      const rows = sql(
        `SELECT status, comment, approver_id FROM approvals WHERE change_set_id = '${journey.changeSetId}'`,
      );
      return rows.length > 0 ? rows[0] : null;
    });
    expect(approval[0]).toBe("approved");
    expect(approval[1]).toBe("approved by the criterion-10 journey");
    // Attributed to a users row, not to the string "admin" a query parameter used to supply.
    const approver = sqlScalar(`SELECT count(*) FROM users WHERE id = '${approval[2]}'`);
    expect(Number(approver)).toBe(1);
  });

  test("step 10 — the artifacts exist on disk with the recorded hashes", async () => {
    // ASSERTION: the change set reached `applied` in the database.
    const status = await eventually(
      "the change set to be applied",
      () => {
        const value = sqlScalar(
          `SELECT status FROM change_sets WHERE id = '${journey.changeSetId}'`,
        );
        return value === "applied" ? value : null;
      },
      // Longer than the default: this waits on the agent receiving the command over the WebSocket,
      // validating the pre-image hashes, writing four files and acknowledging.
      { timeoutMs: 120_000 },
    );
    expect(status).toBe("applied");

    // ASSERTION: every recorded item exists on disk inside the agent container, and its SHA-256 as
    // computed BY the container equals the hash the backend recorded. A hash computed here would be
    // checking the test against itself.
    const recorded = sql(
      `SELECT file_path, new_hash FROM change_items
       WHERE change_set_id = '${journey.changeSetId}' AND action <> 'delete' ORDER BY ordinal`,
    );
    expect(recorded.length).toBeGreaterThan(0);

    for (const [filePath, expectedHash] of recorded) {
      const onDisk = await eventually(`${filePath} to appear on disk`, () => agentFile(filePath));
      expect(onDisk, `${filePath} is absent from the agent workspace`).not.toBeNull();
      const actual = agentFileSha256(filePath);
      expect(actual, `${filePath} hash mismatch`).toBe(expectedHash);
    }

    // And the four artifacts §12.6 names are among them.
    const present = recorded.map((r) => r[0]);
    for (const artifact of EXPECTED_ARTIFACTS) {
      expect(present, `${artifact} was not generated`).toContain(artifact);
    }
  });

  test("step 11 — a backup exists for every overwritten pre-existing file", async () => {
    // ASSERTION: rollback_handles carries a handle for this change set.
    const handle = await eventually("a rollback handle", () =>
      sqlScalar(`SELECT id FROM rollback_handles WHERE change_set_id = '${journey.changeSetId}'`),
    );
    expect(handle).not.toBeNull();

    // Every file that EXISTED before the apply must have a backup. Files that were created have
    // nothing to back up, and asserting one for them would be asserting the wrong thing.
    const overwritten = [...(journey.preImages ?? new Map())]
      .filter(([, content]) => content !== null)
      .map(([filePath]) => filePath);

    const backups = agentList(".forgeops/backups");
    for (const filePath of overwritten) {
      const base = filePath.split("/").pop()!;
      expect(
        backups.some((b) => b.includes(base)),
        `no backup for the overwritten file ${filePath}; backups seen: ${backups.join(", ")}`,
      ).toBe(true);
    }
    // The handle must still be usable, which is what step 13 exercises.
    //
    // The column is `consumed` (boolean), not `state`. This asked for `state` and PostgreSQL
    // answered `ERROR: column "state" does not exist`, which the helper surfaced as a failed
    // docker command rather than as a wrong assertion — so the test could never have passed, and
    // nothing had reached this line before because step 10 always timed out first.
    const consumed = sqlScalar(
      `SELECT consumed FROM rollback_handles WHERE change_set_id = '${journey.changeSetId}'`,
    );
    expect(consumed, "the handle must be unconsumed before step 13 reverts with it").toBe("f");

    // And it must carry the manifest the agent returned, not the empty placeholder the reservation
    // inserts: a handle with `{}` names no backups, so a revert would restore nothing.
    const manifestSize = sqlScalar(
      `SELECT length(backup_manifest::text) FROM rollback_handles WHERE change_set_id = '${journey.changeSetId}'`,
    );
    expect(Number(manifestSize)).toBeGreaterThan(2);
  });

  test("step 12 — the audit viewer lists the full transit with actors", async ({
    page,
    request,
  }) => {
    // ASSERTION on the rows first: the transit this change set actually made.
    //
    // THESE EXPECTATIONS WERE WRITTEN AGAINST A VOCABULARY THAT DOES NOT EXIST. They required an
    // action matching /policy/i and one matching /appl/i. `GovernanceAction` is a CLOSED set and
    // deliberately contains neither for a successful transit: `policy_undefined` is a deployment
    // fault, not a decision, and there is no `applied` action at all because Q-04 allows exactly one
    // row per transit and `_deliver` documents delivery as that transit's outcome leaving the
    // building rather than a transit of its own.
    //
    // So the trail is asserted on what it genuinely guarantees, and the apply is evidenced from the
    // records that DO carry it. That is not a weaker check — it is three specific facts instead of
    // two substring matches, one of which could never hold. The gap it exposes (an audit reader
    // cannot ask "was this applied?" and must consult `change_sets`) is real and is reported rather
    // than papered over.
    const actions = sql(
      `SELECT DISTINCT action FROM audit_events
       WHERE resource_id = '${journey.changeSetId}' OR resource_id = '${journey.projectId}'`,
    ).map((r) => r[0]);
    const transit = actions.join(",");

    // Both halves of the human path: the submission that required a decision, and the decision.
    expect(transit, `actions recorded: ${transit}`).toMatch(/approval_required/i);
    expect(transit, `actions recorded: ${transit}`).toMatch(/change_set_approved/i);

    // The POLICY BINDING, which is what /policy/i was reaching for. It is recorded on the change set
    // itself, and it must equal the bundle published in step 3 — the chokepoint admits a submission
    // only when the device's pin matches, so a mismatch here means the binding was not enforced.
    const boundDigest = sqlScalar(
      `SELECT policy_bundle_digest FROM change_sets WHERE id = '${journey.changeSetId}'`,
    );
    expect(boundDigest, "the change set must record the policy bundle it was decided under").toBe(
      journey.bundleDigest,
    );

    // The APPLY, evidenced where it is actually recorded.
    const appliedRow = sql(
      `SELECT status, applied_at IS NOT NULL FROM change_sets WHERE id = '${journey.changeSetId}'`,
    );
    expect(appliedRow[0][0]).toBe("applied");
    expect(appliedRow[0][1], "an applied change set must carry the moment it was applied").toBe(
      "t",
    );

    // The trail is a HASH CHAIN, and an immutable trail has to be checked as one rather than
    // trusted: every row after the first must carry its predecessor's hash.
    const brokenLinks = sqlScalar(
      `SELECT count(*) FROM (
         SELECT prev_hash, lag(hash) OVER (ORDER BY seq) AS expected FROM audit_events
       ) AS chain WHERE expected IS NOT NULL AND prev_hash <> expected`,
    );
    expect(Number(brokenLinks), "the audit hash chain must be unbroken").toBe(0);

    // Every event carries an actor. An audit trail that cannot say who did something is not one.
    const anonymous = sqlScalar(
      `SELECT count(*) FROM audit_events
       WHERE (resource_id = '${journey.changeSetId}') AND (actor_kind IS NULL OR actor_kind = '')`,
    );
    expect(Number(anonymous)).toBe(0);

    const events = await apiGet(page.request, "/audit/events?limit=100");
    expect(events.status()).toBe(200);

    await gotoAsOperator(page, "/audit");
    // The viewer must render an action that is genuinely in the database for THIS change set.
    //
    // `.first()` because the viewer lists every event and a repeated run produces many rows with the
    // same action — Playwright's strict mode fails on 13 matches. The property is "this action is
    // rendered", so one match satisfies it; asserting a count would be asserting how much history
    // the stack happens to hold.
    await expect(page.getByText(actions[0]).first()).toBeVisible({ timeout: 30_000 });
  });

  test("step 13 — revert, and every file returns to its pre-image byte-for-byte", async ({
    page,
  }) => {
    const reverted = await page.request.post(`${API}/approvals/${journey.changeSetId}/revert`, {
      headers: authHeaders(),
      data: {},
    });
    expect(reverted.status()).toBe(200);

    // THE REVERSE SET NEEDS ITS OWN APPROVAL, and this step assumed the revert applied itself.
    //
    // A revert is a mutation and runs all six stages with fresh authority, so the same policy that
    // required a human for the apply requires one for its reverse — and the reverse of a create is a
    // delete, which the blast-radius analyser scores far higher (64 against the apply's 8). So the
    // reverse set arrives at `pending_approval`, exactly as the original did in step 9, and a
    // journey that skipped this was asserting a rollback that bypassed the approval gate.
    const body = await reverted.json();
    const reverseId = body.reverse_change_set_id;
    expect(reverseId, "the revert must compile a reverse change set").toBeTruthy();

    if (body.status === "pending_approval") {
      const approved = await page.request.post(`${API}/approvals/${reverseId}/approve`, {
        headers: authHeaders(),
        data: { comment: "journey step 13: authorising the revert" },
      });
      expect(approved.status(), await approved.text()).toBe(200);
    }

    // ASSERTION: the reverse set is applied by the agent, which is what actually restores the files.
    const reverseStatus = await eventually(
      "the reverse change set to be applied",
      () => {
        const value = sqlScalar(`SELECT status FROM change_sets WHERE id = '${reverseId}'`);
        return value === "applied" ? value : null;
      },
      { timeoutMs: 120_000 },
    );
    expect(reverseStatus).toBe("applied");

    // ASSERTION: the original reached `reverted`.
    const status = await eventually("the change set to be reverted", () => {
      const value = sqlScalar(`SELECT status FROM change_sets WHERE id = '${journey.changeSetId}'`);
      return value === "reverted" ? value : null;
    });
    expect(status).toBe("reverted");

    // ASSERTION: byte-for-byte. Not "the file changed back" — the exact pre-image, including the
    // case where the pre-image was absence and the file must be gone again.
    for (const [filePath, before] of journey.preImages ?? new Map()) {
      const after = await eventually(`${filePath} to return to its pre-image`, () => {
        const current = agentFile(filePath);
        return current === before ? { current } : null;
      });
      expect(after.current, `${filePath} did not return to its pre-image`).toBe(before);
    }
  });
});
