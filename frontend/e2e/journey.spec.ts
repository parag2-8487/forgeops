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
import {
  gotoAsOperator,
  mintAccessToken as mintToken,
  OPERATOR,
  SESSION_STATE_PATH,
  signIn,
} from "./helpers/auth";

test.describe.configure({ mode: "serial" });

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8000/api/v1";

/** Carried between steps. Populated as the journey proceeds. */

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
  // Delegates to `helpers/auth.ts` so the paint spec can mint one the same way; this wrapper exists
  // only to cache the result on the journey's shared state, which the later steps read.
  journey.accessToken = await mintToken(page, API);
  return journey.accessToken;
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
    // AND THE APPLY'S OWN BACKUPS. `mutate` copies a pre-existing target to
    // `<name>.backup.<timestamp>` BESIDE it before overwriting, so a run that overwrites the
    // Dockerfile a previous run created leaves one behind. They accumulate in the checkout — four had
    // built up during this work — and `git status` noise from a test run is a defect in the test.
    composeExec(
      "agent",
      ["sh", "-c", "find /workspace -name '*.backup.*' -maxdepth 2 -delete || true"],
      { allowFailure: true },
    );

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
    // THE SCAN COMES FIRST, and that is this step's substance rather than a setup detail.
    //
    // Readiness reads `file_tree` and `file_contents` now, not `projects.settings`. It used to score
    // a dict the caller built out of operator-entered configuration, so the number described what
    // somebody had typed rather than what the repository contained -- and it scored a project that
    // had never been scanned at all. With the index as the source an unscanned project honestly
    // scores zero with `indexed=false`, so criterion 2 -- "agent scans codebase and produces
    // readiness score" -- is only satisfied if a scan actually ran. This is where it runs.
    //
    // Through the agent's own CLI, because the agent OWNS the workspace: it is the only party that
    // can read the files. The write is still authorised server-side -- the index endpoint requires
    // the principal and scopes by tenant -- so this cannot index into a project the device may not
    // see.
    const scan = composeExec("agent", ["forgeops-agent", "scan", "--project", journey.projectId!]);
    const indexedFiles = Number(/indexed (\d+) file/.exec(scan)?.[1] ?? 0);
    expect(indexedFiles, `the agent indexed nothing. Its output was:\n${scan}`).toBeGreaterThan(0);

    // ASSERTION on the API first, so the chart is checked against known numbers.
    const report = await apiGet(page.request, `/projects/${journey.projectId}/readiness`);
    expect(report.status()).toBe(200);
    const readiness = await report.json();

    // phases.md 1.4's six weighted categories. The previous five were the settings-derived set,
    // which had no `orchestration` and no `iac` category at all -- so a project with no Kubernetes
    // manifests and no Terraform scored identically to one carrying both.
    expect(Object.keys(readiness.categories).sort()).toEqual([
      "ci_config_score",
      "containerization_score",
      "env_config_score",
      "iac_score",
      "orchestration_score",
      "security_policy_score",
    ]);
    // The score has to come from files, so it must say how many it read -- and the count must be
    // the one the scan reported, not merely non-zero.
    expect(Number(readiness.evaluated_paths)).toBe(indexedFiles);
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
    // A REAL MODEL CALL, so a real model's latency.
    //
    // This step used to render a template, which returned in milliseconds. It now goes through the
    // six-tier router to a live `qwen2.5-coder:1.5b`, and the deterministic gate may ask for up to
    // three iterations before it accepts the artifacts — so several minutes on CPU is ordinary, not
    // a symptom. The observed cost of one provider run in the integration suite is 200-340s.
    //
    // Raising a TIMEOUT is not weakening an assertion: nothing about what the step checks changes,
    // and every assertion below still has to hold. The alternative — capping iterations to fit a
    // 180s budget — would change the product's behaviour to suit the test, which is the wrong way
    // round.
    //
    // 1200s rather than 600s: the observed cost is 528s when the deterministic gate accepts the
    // first attempt and beyond 600s when it asks for another, so 600 was a coin toss — it passed
    // once and timed out on the next run with no code change between them. The budget covers three
    // attempts with margin, because a flaky timeout teaches people to re-run rather than to read.
    test.setTimeout(1_200_000);
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
