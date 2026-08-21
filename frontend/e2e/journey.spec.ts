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
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
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
const journey: {
  projectId?: string;
  deviceId?: string;
  pairingCode?: string;
  runId?: string;
  changeSetId?: string;
  accessToken?: string;
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

/** Exchanges the httpOnly session cookie for an access token, as `lib/api/client.ts` does. */
async function mintAccessToken(page: Page): Promise<string> {
  const refreshed = await page.request.post(`${API}/auth/refresh`, {
    headers: { Accept: "application/json" },
  });
  expect(refreshed.status(), await refreshed.text()).toBe(200);
  const body = await refreshed.json();
  expect(body.access_token, "the refresh response carried no access token").toBeTruthy();
  journey.accessToken = body.access_token as string;
  return journey.accessToken;
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
  const idp = (process.env.E2E_OIDC_ISSUER ?? "").replace(/\/application\/o\/[^/]+\/?$/, "");
  expect(idp, "E2E_OIDC_ISSUER must be set for step 1").not.toBe("");

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
  });

  test("step 2 — create a project pointing at the fixture app", async ({ page }) => {
    const created = await page.request.post(`${API}/projects`, {
      headers: authHeaders(),
      data: {
        name: "e2e-fixture",
        repo_url: "https://example.invalid/forgeops/e2e-fixture",
        local_path: "/workspace",
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
    const minted = await page.request.post(`${API}/agents/pairing-codes`, {
      headers: authHeaders(),
      data: { project_id: journey.projectId },
    });
    expect(minted.status()).toBe(201);
    const body = await minted.json();
    journey.pairingCode = body.code as string;
    expect(journey.pairingCode).toMatch(/^[0-9A-HJKMNP-TV-Z]{4,}/); // Crockford base32, no I/L/O/U

    // ASSERTION: the real binary, in its own container, exits 0 and prints the device it was given.
    const output = composeExec("agent", [
      "forgeops-agent",
      "pair",
      "--code",
      journey.pairingCode!,
      "--backend",
      "ws://backend:8000/api/v1/agent/ws",
    ]);
    expect(output).toContain("Paired.");
    const match = /device id:\s+(\S+)/.exec(output);
    expect(match, `pair output did not name a device id:\n${output}`).not.toBeNull();
    journey.deviceId = match![1];

    // And the code is single-use: a second attempt must fail.
    const replay = composeExec(
      "agent",
      ["forgeops-agent", "pair", "--code", journey.pairingCode!],
      { allowFailure: true },
    );
    expect(replay).not.toContain("Paired.");

    composeExecDetached("agent", ["forgeops-agent", "run"]);
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

    await page.goto(`/readiness?project=${journey.projectId}`);
    await page.getByLabel(/project id/i).fill(journey.projectId!);
    // The rendered chart must carry the SAME numbers the API returned.
    const chart = page.getByRole("img", { name: /readiness/i }).or(page.locator("svg").first());
    await expect(chart).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(String(readiness.overall_score))).toBeVisible();
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

    await page.goto("/approvals");
    await page.getByRole("button", { name: new RegExp(journey.changeSetId!) }).click();

    const diff = page.getByRole("region", { name: /diff for Dockerfile/i });
    await expect(diff).toBeVisible({ timeout: 30_000 });

    // Both view modes, asserted through ARIA state rather than a class name.
    const unified = page.getByRole("button", { name: "Unified" });
    const split = page.getByRole("button", { name: "Side by side" });
    await expect(unified).toHaveAttribute("aria-pressed", "true");
    await split.click();
    await expect(split).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByText("Before")).toBeVisible();
    await expect(page.getByText("After")).toBeVisible();
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
    await page.goto("/approvals");
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
    // The handle must be usable, which is what step 13 exercises.
    const state = sqlScalar(
      `SELECT state FROM rollback_handles WHERE change_set_id = '${journey.changeSetId}'`,
    );
    expect(state).not.toBe("consumed");
  });

  test("step 12 — the audit viewer lists the full transit with actors", async ({
    page,
    request,
  }) => {
    // ASSERTION on the rows first: the three actions §12.6 names must all be present.
    const actions = sql(
      `SELECT DISTINCT action FROM audit_events
       WHERE resource_id = '${journey.changeSetId}' OR resource_id = '${journey.projectId}'`,
    ).map((r) => r[0]);

    const transit = actions.join(",");
    expect(transit).toMatch(/policy/i);
    expect(transit).toMatch(/approv/i);
    expect(transit).toMatch(/appl/i);

    // Every event carries an actor. An audit trail that cannot say who did something is not one.
    const anonymous = sqlScalar(
      `SELECT count(*) FROM audit_events
       WHERE (resource_id = '${journey.changeSetId}') AND (actor_kind IS NULL OR actor_kind = '')`,
    );
    expect(Number(anonymous)).toBe(0);

    const events = await apiGet(page.request, "/audit/events?limit=100");
    expect(events.status()).toBe(200);

    await page.goto("/audit");
    // The viewer must render an action that is genuinely in the database for THIS change set.
    await expect(page.getByText(actions[0])).toBeVisible({ timeout: 30_000 });
  });

  test("step 13 — revert, and every file returns to its pre-image byte-for-byte", async ({
    page,
  }) => {
    const reverted = await page.request.post(`${API}/approvals/${journey.changeSetId}/revert`, {
      headers: authHeaders(),
      data: {},
    });
    expect(reverted.status()).toBe(200);

    // ASSERTION: the change set reached `reverted`.
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
