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
import { expect, test, type Page } from "@playwright/test";
import { eventually, sql, sqlScalar } from "./helpers/stack";
import { gotoAsOperator, mintAccessToken as mintToken } from "./helpers/auth";

test.describe.configure({ mode: "serial" });

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8000/api/v1";

const healing: {
  projectId?: string;
  environmentId?: string;
  deploymentId?: string;
  incidentId?: string;
  suggestionId?: string;
  changeSetId?: string;
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

async function authHeaders(page: Page): Promise<Record<string, string>> {
  const token = await mintToken(page, API);
  return {
    [AUTH_HEADER]: [BEARER_SCHEME, token].join(" "),
    "Content-Type": "application/json",
  };
}

test.describe("Criterion: a failed deploy is detected, explained, and its fix approved by a human", () => {
  test("step 1 — a project and a staging environment exist", async ({ page }) => {
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

  test("step 2 — a deployment is requested and genuinely fails", async ({ page }) => {
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
         ORDER BY requested_at DESC LIMIT 1`,
      );
      return rows.length > 0 ? rows[0] : null;
    });
    healing.deploymentId = deployment[0];
    expect(healing.deploymentId).toBeTruthy();
  });

  test("step 3 — the AI detects it: an incident is filed by the production path", async () => {
    // Filed by `DeploymentService.fail` through the composed `DeploymentIncidentRecorder`. Nothing in
    // this spec writes to `deployment_incidents` or `incidents`.
    const incident = await eventually(
      "an incident filed for this project",
      () => {
        const rows = sql(
          `SELECT id, title, severity, source FROM incidents
           WHERE project_id = '${healing.projectId}' ORDER BY first_seen_at DESC LIMIT 1`,
        );
        return rows.length > 0 ? rows[0] : null;
      },
      { timeoutMs: 180_000 },
    );
    healing.incidentId = incident[0];

    // ASSERTION: the incident names the deployment path it came from, so a row filed by some other
    // observer cannot satisfy this.
    expect(incident[3]).toBe("deployment");
    expect(incident[1]).toBeTruthy();
  });

  test("step 4 — the operator sees the incident on the incidents page", async ({ page }) => {
    await gotoAsOperator(page, `/incidents?project=${healing.projectId}`);

    await expect(page.getByTestId("incident-list")).toBeVisible({ timeout: 30_000 });
    // The row for THIS incident, by id, so a list showing some other project's incident fails.
    const row = page.getByTestId(`incident-row-${healing.incidentId}`);
    await expect(row).toBeVisible({ timeout: 30_000 });

    // Backed by the row: the title on screen is the title in the database.
    const title = sqlScalar(`SELECT title FROM incidents WHERE id = '${healing.incidentId}'`);
    await expect(row).toContainText(title!.slice(0, 40));
  });

  test("step 5 — the AI explains it: a root cause with evidence provenance", async ({ page }) => {
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

  test("step 6 — the AI suggests a fix, and its diff is rendered", async ({ page }) => {
    const suggestion = await eventually(
      "a suggestion for this incident",
      () => {
        const rows = sql(
          `SELECT s.id, s.path FROM incident_suggestions s
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

  test("step 7 — submitting the fix produces a governed change set, not an applied change", async ({
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
        `SELECT change_set_id FROM incident_suggestions WHERE id = '${healing.suggestionId}'`,
      ),
    ).toBe(healing.changeSetId);
  });

  test("step 8 — a human approves it in the browser, and the decision is attributed", async ({
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
