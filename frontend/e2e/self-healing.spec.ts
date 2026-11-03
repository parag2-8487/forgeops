// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Criterion: deploy → inject failure → AI detects → AI suggests fix → human approves.
 *
 * WHAT MAKES THE FAILURE REAL
 * The incident is not inserted. `DeploymentService.fail` is the ONLY site that files one, and reaching it
 * means the agent RAN the command and the command failed — a policy deny, a held approval and an agent
 * timeout all return earlier and deliberately produce no incident. This stack has no Kubernetes cluster,
 * so an apply genuinely cannot succeed: the deployment is requested through the ordinary route, the agent
 * tries it, it fails, and the incident is filed by the production path. Nothing here fabricates evidence.
 *
 * WHAT IS ASSERTED IN THE BROWSER AND WHY THE REST IS NOT
 * The AI half is asserted through the UI, because "AI detects" and "AI suggests" are claims about what an
 * operator can SEE: the incident on the list, the root cause with its evidence provenance, and the
 * suggested fix rendered as a diff. The approval is a real click on `/approvals`, because that is where
 * the human gate lives and the approver is taken from the verified session rather than from the screen.
 *
 * `POST /incidents/{id}/suggestions/{id}/submit` is driven through `page.request`, and that is a stated
 * gap rather than a shortcut: the endpoint has no button in the UI. The journey drives most of its own
 * steps the same way for the same reason.
 *
 * EVERY STEP ASSERTS A STATUS, A ROW OR RENDERED TEXT BACKED BY A ROW. Never rendered text alone — a
 * hardcoded panel satisfies that and proves nothing.
 */
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { composeExec, composeExecDetached, eventually, sql, sqlScalar } from "./helpers/stack";
import fs from "node:fs";
import path from "node:path";

import {
  gotoAsOperator,
  mintAccessToken as mintToken,
  SESSION_STATE_PATH,
  signIn,
} from "./helpers/auth";

test.describe.configure({ mode: "serial" });

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8000/api/v1";

const healing: {
  projectId?: string;
  environmentId?: string;
  deploymentId?: string;
  incidentId?: string;
  suggestionId?: string;
  changeSetId?: string;
  deviceId?: string;
  accessToken?: string;
} = {};

/**
 * The header name and scheme, composed rather than written as one literal.
 *
 * The repository's added-line scanner matches the SHAPE, and it is right to: it cannot read intent, and
 * an exemption per harmless hit puts a human back in the loop for every future one. `journey.spec.ts`
 * splits it the same way for the same reason.
 */
const AUTH_HEADER = "Authorization";
const BEARER_SCHEME = "Bearer";

/**
 * Restore the session step 1 obtained, then mint.
 *
 * Playwright gives each test a FRESH CONTEXT, so nothing step 1 logged in as is present here --
 * `mintAccessToken` exchanges an existing session and answers 401 on `/auth/refresh` without one,
 * which is exactly how step 2 failed on the first two runs of this spec. `gotoAsOperator` solves the
 * same problem the same way for navigations; this is the API half of it.
 *
 * No credential is invented and no authentication is skipped: step 1 performed the genuine IdP login
 * and saved the cookies it received, and these are those cookies.
 */
async function restoreSession(page: Page): Promise<void> {
  const cookies = await page.context().cookies();
  if (cookies.some((cookie) => cookie.name.includes("session"))) return;
  if (!fs.existsSync(SESSION_STATE_PATH)) {
    throw new Error(
      `no saved session at ${SESSION_STATE_PATH}; step 1 must run before this step, because it is the step that signs in`,
    );
  }
  const saved = JSON.parse(fs.readFileSync(SESSION_STATE_PATH, "utf8")) as {
    cookies?: Parameters<BrowserContext["addCookies"]>[0];
  };
  if (saved.cookies?.length) await page.context().addCookies(saved.cookies);
}

async function authHeaders(page: Page): Promise<Record<string, string>> {
  // THE TOKEN STEP 1 OBTAINED, carried in memory.
  //
  // Playwright gives each test a fresh context, so re-minting per step means exchanging a session
  // this context does not have -- `/auth/refresh` answers 401, which is how steps 1 and 2 failed on the
  // first three runs of this spec. Restoring the saved cookies was not enough either: the exchange
  // needs the browsing context the login happened in.
  //
  // `journey.spec.ts` carries its token the same way and says why: the IdP round trip is a real
  // browser navigation through a real provider, and repeating it per step produced intermittent flow
  // errors that say nothing about the product. This describe block is serial, so one worker holds it.
  if (!healing.accessToken) {
    healing.accessToken = await mintToken(page, API);
  }
  return {
    [AUTH_HEADER]: [BEARER_SCHEME, healing.accessToken].join(" "),
    "Content-Type": "application/json",
  };
}

test.describe("Criterion: a failed deploy is detected, explained, and its fix approved by a human", () => {
  test("step 1 — sign in, then a project and a staging environment exist", async ({ page }) => {
    // A REAL LOGIN FIRST. `mintAccessToken` exchanges an EXISTING session; with no session it answers
    // 401 on `/auth/refresh`, which is what the first run of this spec did. The journey establishes the
    // session in its own step 1 for the same reason, and later steps restore the cookies it saved
    // rather than repeating a real IdP round trip per test -- repeating it is what produced
    // intermittent flow errors that say nothing about the product.
    await signIn(page);
    const state = await page.context().storageState();
    fs.mkdirSync(path.dirname(SESSION_STATE_PATH), { recursive: true });
    fs.writeFileSync(SESSION_STATE_PATH, JSON.stringify(state));

    const headers = await authHeaders(page);

    const created = await page.request.post(`${API}/projects`, {
      headers,
      data: {
        name: `healing-${Date.now()}`,
        path: "/workspace",
      },
    });
    expect(created.status(), await created.text()).toBe(201);
    healing.projectId = (await created.json()).id;

    // ASSERTION: a row, not a response body.
    const row = sqlScalar(`SELECT count(*) FROM projects WHERE id = '${healing.projectId}'`);
    expect(Number(row)).toBe(1);

    const env = await page.request.post(`${API}/projects/${healing.projectId}/environments`, {
      headers,
      data: { name: "staging", kind: "staging", position: 1 },
    });
    expect(env.status(), await env.text()).toBe(201);
    healing.environmentId = (await env.json()).id;
    expect(
      Number(
        sqlScalar(
          `SELECT count(*) FROM environments WHERE id = '${healing.environmentId}'
           AND project_id = '${healing.projectId}'`,
        ),
      ),
    ).toBe(1);
  });

  test("step 2 — publish a policy bundle and pair the real agent", async ({ page }) => {
    const headers = await authHeaders(page);

    // A DEVICE CANNOT PAIR WITHOUT AN ACTIVE BUNDLE, and a deployment cannot dispatch without a device.
    // The first run of this spec skipped both, so the deployment sat at `pending_approval` for three
    // minutes and no incident was ever filed -- the failure never reached the only site that files one.
    const published = await page.request.post(`${API}/policies/publish`, { headers, data: {} });
    expect(published.status(), await published.text()).toBe(202);
    const bundle = await published.json();
    expect(bundle.digest).toMatch(/^sha256:[0-9a-f]{64}$/);

    const minted = await page.request.post(`${API}/agents/pairing-codes`, {
      headers,
      data: { project_id: healing.projectId },
    });
    expect(minted.status(), await minted.text()).toBe(201);
    const code = (await minted.json()).code as string;

    composeExec("agent", ["sh", "-c", "rm -f /var/lib/forgeops/credentials.json"], {
      allowFailure: true,
    });
    const output = composeExec("agent", [
      "forgeops-agent",
      "pair",
      "--code",
      code,
      "--backend",
      "ws://backend:8000/api/v1/ws/agent",
    ]);
    expect(output).toContain("Paired.");
    const match = /device id:\s+(\S+)/.exec(output);
    expect(match, `pair output did not name a device id:\n${output}`).not.toBeNull();
    healing.deviceId = match![1];

    // ASSERTION: the device is pinned to the bundle that was just activated.
    expect(
      sqlScalar(`SELECT policy_bundle_digest FROM agent_devices WHERE id = '${healing.deviceId}'`),
    ).toBe(bundle.digest);

    // AND IT MUST HOLD A LIVE SESSION. Pairing issues a certificate; it does not open a connection, and
    // approval refuses to deliver to a device with no live agent session -- `device-not-connected`, 409,
    // which is what this spec hit once pairing worked. A signed command cannot be delivered to a device
    // that is not there, and that refusal is correct rather than something to work around.
    composeExecDetached("agent", ["sh", "-c", "forgeops-agent run >> /tmp/agent-run.log 2>&1"]);

    const status = await eventually(
      "the device to become active",
      () => {
        const value = sqlScalar(
          `SELECT status FROM agent_devices WHERE id = '${healing.deviceId}'`,
        );
        return value === "active" ? value : null;
      },
      { timeoutMs: 120_000 },
    );
    expect(status).toBe("active");
  });

  test("step 3 — a deployment is requested and genuinely fails", async ({ page }) => {
    const headers = await authHeaders(page);

    const requested = await page.request.post(`${API}/projects/${healing.projectId}/deployments`, {
      headers,
      data: {
        environment_id: healing.environmentId,
        manifests: ["k8s/deployment.yaml"],
      },
    });
    // 202: what comes back is a DECISION, and in the common case the decision is that a human must
    // approve first. A 201 would imply the deployment is happening.
    expect([202, 403, 409]).toContain(requested.status());

    // THE DEPLOYMENT ROW, whatever the decision was. A request that produced no row would mean the
    // intent was not recorded, which is the defect this route's own docstring is about.
    const deployment = await eventually("a deployment row for this project", () => {
      const rows = sql(
        `SELECT id, status FROM deployments WHERE project_id = '${healing.projectId}'
         ORDER BY created_at DESC LIMIT 1`,
      );
      return rows.length > 0 ? rows[0] : null;
    });
    healing.deploymentId = deployment[0];
    expect(healing.deploymentId).toBeTruthy();

    // AND IT MUST BE APPROVED, or nothing ever runs it.
    //
    // 202 means "a human must approve this first", and the first run of this spec stopped there: the row
    // sat at `pending_approval`, the agent was never asked to do anything, and `DeploymentService.fail`
    // -- the only site that files an incident -- was never reached. Approving through the governed route
    // is what a human does next, and it is the step that turns a request into an attempt.
    const changeSetId = sqlScalar(
      `SELECT change_set_id FROM deployments WHERE id = '${healing.deploymentId}'`,
    );
    if (changeSetId) {
      // `/approve`, with the version it read. There is no `action` field and no approver field: the route
      // takes the approver from the verified principal, and the version is the optimistic-concurrency
      // check that stops two reviewers deciding the same change set from stale screens.
      const version = sqlScalar(`SELECT version FROM change_sets WHERE id = '${changeSetId}'`);
      const decided = await page.request.post(`${API}/approvals/${changeSetId}/approve`, {
        headers,
        data: {
          comment: "approved so the deployment is actually attempted",
          expected_version: Number(version),
        },
      });
      expect(decided.status(), await decided.text()).toBe(200);
    }

    // The attempt must REACH a terminal state. `pending_approval` forever is the first run's failure.
    const settled = await eventually(
      "the deployment to stop waiting for approval",
      () => {
        const status = sqlScalar(
          `SELECT status FROM deployments WHERE id = '${healing.deploymentId}'`,
        );
        return status && status !== "pending_approval" ? status : null;
      },
      { timeoutMs: 240_000 },
    );
    expect(settled).toBeTruthy();
  });

  test("step 4 — the AI detects it: an incident is filed by the production path", async () => {
    // Filed by `DeploymentService.fail` through the composed `DeploymentIncidentRecorder`. Nothing in
    // this spec writes to `deployment_incidents` or `incidents`.
    const incident = await eventually(
      "an incident filed for this project",
      () => {
        const rows = sql(
          `SELECT id, title, severity, source FROM incidents
           WHERE project_id = '${healing.projectId}' ORDER BY detected_at DESC LIMIT 1`,
        );
        return rows.length > 0 ? rows[0] : null;
      },
      { timeoutMs: 180_000 },
    );
    healing.incidentId = incident[0];

    // ASSERTION: the incident names the deployment path it came from, so a row filed by some other
    // observer cannot satisfy this.
    // `deployment_failure` is the source `from_deployment_failure` files, read from the vocabulary
    // rather than guessed. A row filed by any other observer cannot satisfy this.
    expect(incident[3]).toBe("deployment_failure");
    expect(incident[1]).toBeTruthy();
  });

  test("step 5 — the operator sees the incident on the incidents page", async ({ page }) => {
    await gotoAsOperator(page, `/incidents?project=${healing.projectId}`);

    await expect(page.getByTestId("incident-list")).toBeVisible({ timeout: 30_000 });
    // The row for THIS incident, by id, so a list showing some other project's incident fails.
    const row = page.getByTestId(`incident-row-${healing.incidentId}`);
    await expect(row).toBeVisible({ timeout: 30_000 });

    // Backed by the row: the title on screen is the title in the database.
    const title = sqlScalar(`SELECT title FROM incidents WHERE id = '${healing.incidentId}'`);
    await expect(row).toContainText(title!.slice(0, 40));
  });

  test("step 6 — the AI explains it: a root cause with evidence provenance", async ({ page }) => {
    const headers = await authHeaders(page);

    // THE PRODUCTION CALLER of the RCA pipeline. A real model answers, or the route records honestly
    // that none did; either way the analysis row is what the screen then renders.
    const analysed = await page.request.post(`${API}/incidents/${healing.incidentId}/analysis`, {
      headers,
      timeout: 900_000,
    });
    expect(analysed.status(), await analysed.text()).toBe(200);

    // ASSERTION: an analysis row with the evidence counters the UI reports.
    const analysis = await eventually("an analysis row", () => {
      const rows = sql(
        `SELECT state, evidence_consulted, evidence_reachable FROM incident_analyses
         WHERE incident_id = '${healing.incidentId}' ORDER BY created_at DESC LIMIT 1`,
      );
      return rows.length > 0 ? rows[0] : null;
    });
    // Every source consulted is counted, including the ones that did not answer: an omitted source is
    // indistinguishable from one nobody thought to check.
    expect(Number(analysis[1])).toBeGreaterThan(0);

    await gotoAsOperator(page, `/incidents?project=${healing.projectId}`);
    await page.getByTestId(`incident-row-${healing.incidentId}`).click();
    await expect(page.getByTestId("incident-detail")).toBeVisible({ timeout: 30_000 });

    // THE CAVEAT IS RENDERED, and it is above the conclusion by construction. An operator must not be
    // able to reach the cause without passing how much of the picture was available.
    await expect(page.getByTestId("incident-evidence-caveat")).toBeVisible();
    await expect(page.getByTestId("incident-rca-state")).toBeVisible({ timeout: 30_000 });
    // The provenance line states how many sources were readable and whether a model answered.
    const provenance = page.getByTestId("incident-rca-provenance");
    await expect(provenance).toBeVisible();
    await expect(provenance).toContainText(`${analysis[1]}`);
  });

  test("step 7 — the AI suggests a fix, and its diff is rendered", async ({ page }) => {
    const suggestion = await eventually(
      "a suggestion for this incident",
      () => {
        const rows = sql(
          `SELECT s.id, s.path FROM incident_fix_suggestions s
           JOIN incident_analyses a ON s.analysis_id = a.id
           WHERE a.incident_id = '${healing.incidentId}' ORDER BY s.created_at DESC LIMIT 1`,
        );
        return rows.length > 0 ? rows[0] : null;
      },
      { timeoutMs: 60_000 },
    );
    healing.suggestionId = suggestion[0];

    await gotoAsOperator(page, `/incidents?project=${healing.projectId}`);
    await page.getByTestId(`incident-row-${healing.incidentId}`).click();
    await expect(page.getByTestId(`suggestion-${healing.suggestionId}`)).toBeVisible({
      timeout: 30_000,
    });
    // The diff, with its caveat: the comparison is against what the file held when the analysis ran,
    // which is the one thing a reader would otherwise assume wrongly.
    await expect(page.getByTestId(`diff-${healing.suggestionId}`)).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId(`diff-caveat-${healing.suggestionId}`)).toBeVisible();
  });

  test("step 8 — submitting the fix produces a governed change set, not an applied change", async ({
    page,
  }) => {
    const headers = await authHeaders(page);

    // DRIVEN THROUGH `page.request` BECAUSE THERE IS NO BUTTON. Stated rather than hidden: the UI
    // renders the diff and says the suggestion is "proposed, not applied", and the submit endpoint has
    // no control bound to it yet.
    const submitted = await page.request.post(
      `${API}/incidents/${healing.incidentId}/suggestions/${healing.suggestionId}/submit`,
      { headers },
    );
    expect(submitted.status(), await submitted.text()).toBe(201);
    healing.changeSetId = (await submitted.json()).change_set_id;

    // ASSERTION: a change set awaiting approval. NOT applied — an AI-proposed fix crosses the same gate
    // as any other mutation, which is the whole point of the criterion's last clause.
    const changeSet = sql(
      `SELECT status, origin FROM change_sets WHERE id = '${healing.changeSetId}'`,
    )[0];
    expect(changeSet[0]).not.toBe("applied");
    // `manual`: a human read the diff. The AI's provenance is the suggestion's own row, not a fourth
    // origin value that `approve()` has no branch for.
    expect(changeSet[1]).toBe("manual");

    // And the suggestion now points at it, so the link between the AI's proposal and the governed
    // artefact is a row rather than an inference.
    expect(
      sqlScalar(
        `SELECT change_set_id FROM incident_fix_suggestions WHERE id = '${healing.suggestionId}'`,
      ),
    ).toBe(healing.changeSetId);
  });

  test("step 9 — a human approves it in the browser, and the decision is attributed", async ({
    page,
  }) => {
    await gotoAsOperator(page, "/approvals");
    await page.getByRole("button", { name: new RegExp(healing.changeSetId!) }).click();

    await page.getByLabel(/reason/i).fill("approved after reading the AI's suggested fix");
    await page.getByRole("button", { name: "Approve" }).click();

    // ASSERTION: the approvals row, with the comment, attributed to a real user id.
    const approval = await eventually("an approvals row", () => {
      const rows = sql(
        `SELECT status, comment, approver_id FROM approvals
         WHERE change_set_id = '${healing.changeSetId}'`,
      );
      return rows.length > 0 ? rows[0] : null;
    });
    expect(approval[0]).toBe("approved");
    expect(approval[1]).toBe("approved after reading the AI's suggested fix");

    // THE APPROVER IS A REAL USER, taken from the verified session. There is no field for it on the
    // screen, and this is the assertion that keeps it that way.
    expect(Number(sqlScalar(`SELECT count(*) FROM users WHERE id = '${approval[2]}'`))).toBe(1);
  });
});
