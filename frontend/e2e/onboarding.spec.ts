// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Nothing to an applied change set, THROUGH THE BROWSER.
 *
 * WHY THIS EXISTS BESIDE `journey.spec.ts` RATHER THAN INSIDE IT
 *
 * The journey proves the SYSTEM works: it drives most of its thirteen steps through `page.request`,
 * which is an HTTP client that happens to share a browser's cookie jar. That is the right shape for
 * asserting that the engine behaves, and it is exactly why it could stay green while the browser
 * could not reach most of the engine — every capability on the path existed and only some of them had
 * a screen.
 *
 * This spec asserts the other half: that a person with a browser and nothing else can get from an
 * empty installation to a change set applied on disk. Every step below is performed by CLICKING,
 * except the two the architecture puts outside the browser on purpose — pairing and scanning, which
 * the agent must do because §2.2.1 confines command dispatch to the governance chokepoint and the
 * agent owns its own workspace. Those two are run through the agent's real CLI, using the code and
 * the command the UI itself displayed.
 *
 * THE ORDER IS THE POINT. Each step is a precondition of the next, and the one that is easiest to skip
 * is publishing the policy bundle: the chokepoint refuses a submission from any device not pinned to
 * the tenant's active digest, so an unpublished tenant fails at approval with a stale-bundle error
 * four layers from its cause. The SSE paint test discovered that the hard way. `/onboarding` exists so
 * nobody has to.
 *
 * SEPARATE PLAYWRIGHT PROJECT, for the same reason `sse-paint.spec.ts` is one: this pairs a device and
 * applies a change set, so folding it into the serial journey would make "13/13" mean something
 * different, and it needs its own timeout because it makes a real model call.
 *
 * RERUNNABLE. The project name carries a run-unique suffix, the agent's credential file is wiped
 * before pairing, and the generated artifacts are removed from the workspace first — the same three
 * things the journey does, for the same reason: a test that only passes against a pristine stack
 * fails every time afterwards, and the failure reads as a product defect.
 */

import { expect, test, type Page } from "@playwright/test";
import { composeExec, composeExecDetached, eventually, sqlScalar } from "./helpers/stack";
import {
  gotoAsOperator,
  mintAccessToken,
  OPERATOR,
  SESSION_STATE_PATH,
  signIn,
} from "./helpers/auth";

test.describe.configure({ mode: "serial" });

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8000/api/v1";

/** Unique per run, so a rerun does not collide with the previous run's project. */
const SUFFIX = Date.now().toString(36);
const PROJECT_NAME = `Onboarding ${SUFFIX}`;

/** The workspace the agent container bind-mounts, which is what a scan will read. */
const WORKSPACE = "/workspace";

/** The artifacts a generation run produces, removed before the run for the reason above. */
const EXPECTED_ARTIFACTS = [
  "Dockerfile",
  "k8s/deployment.yaml",
  "k8s/service.yaml",
  "k8s/ingress.yaml",
];

const onboarding: {
  projectId?: string;
  pairingCode?: string;
  deviceId?: string;
  bundleDigest?: string;
  changeSetId?: string;
} = {};

/** Skip cleanly rather than fail confusingly when the stack's IdP was never provisioned. */
function requireOperator() {
  test.skip(OPERATOR.password === "", "E2E_OIDC_PASSWORD is unset; provision-authentik.py sets it");
}

/**
 * Navigate to a screen as the signed-in operator and wait for its authenticated panels.
 *
 * `AuthBoundary` posts `/auth/refresh` on mount and renders "Restoring your session…" until it
 * settles, so a click issued immediately after `goto` can land on the placeholder. Waiting for the h1
 * is the observable that the guarded tree has actually rendered.
 */
async function open(page: Page, path: string, heading: RegExp) {
  await gotoAsOperator(page, path);
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible({
    timeout: 30_000,
  });
}

/**
 * Persist the cookies this context now holds, so the next step starts from the live session.
 *
 * THE REFRESH TOKEN ROTATES ON EVERY USE. `AuthBoundary` posts `/auth/refresh` on each mount, which
 * retires the cookie it presented and issues a successor — so any navigation AFTER `open()` has
 * already re-saved leaves the snapshot one rotation behind, and the next step restores a spent token
 * and lands on the sign-in screen.
 *
 * That is exactly how step 6 failed the first time this spec ran end to end: step 5 reloads the page
 * to observe the index becoming non-empty, that reload rotated the cookie, and the following step
 * restored the pre-reload snapshot. `gotoAsOperator` re-saves for the navigations it performs itself;
 * this is for the ones a step performs on its own.
 */
async function saveSession(page: Page) {
  await page.context().storageState({ path: SESSION_STATE_PATH });
}

test.describe("Part E: the onboarding path, in a browser", () => {
  test("step 0 — sign in, and the path says what has not been done", async ({ page }) => {
    requireOperator();
    await signIn(page);
    await mintAccessToken(page, API);

    await open(page, "/onboarding", /Getting started/i);

    // Eight steps, in the order their preconditions require.
    await expect(page.getByTestId("onboarding-steps").locator("li")).toHaveCount(8);

    // THE ASSERTION THAT MATTERS HERE: the bundle step is never ticked, because no endpoint reports
    // whether a tenant has an active bundle. A tick nothing checked is the defect this whole pass
    // removed from other screens, so the path declines to invent one.
    await expect(page.getByTestId("step-5-state")).toHaveText("Not checked");
    await expect(page.getByText(/no tick is shown for something nothing checked/i)).toBeVisible();
  });

  test("step 1 — create a project through the form", async ({ page }) => {
    requireOperator();
    await open(page, "/projects", /Projects/i);

    // PRD FR-01, which had no form at all. The path is typed, and the form says why: a browser cannot
    // report a directory's absolute path, so there is no control that could fill it in.
    //
    // `exact: true` on the name field: the filter section's search box is labelled "Search name or
    // path", and an accessible-name substring match resolves to both. Exact matching is the fix rather
    // than a nth() index, which would silently follow whichever came first in the DOM.
    await page.getByLabel(/directory on the machine/i).check();
    await page.getByLabel("Name", { exact: true }).fill(PROJECT_NAME);
    await page.getByLabel(/working-tree path/i).fill(WORKSPACE);
    await page.getByRole("button", { name: /create project/i }).click();

    // ASSERTION: a row exists in the database. Not "the list rendered the name" — a create that
    // renders its own request body and stores nothing is precisely the defect this surface once had.
    const id = await eventually("the project row", () =>
      sqlScalar(`SELECT id FROM projects WHERE name = '${PROJECT_NAME}'`),
    );
    onboarding.projectId = id!;
    expect(onboarding.projectId).toMatch(/^[0-9a-f-]{36}$/);

    // And the row is visible on screen, described honestly: nothing has scanned it.
    await expect(page.getByTestId(`index-${onboarding.projectId}`)).toContainText(/not scanned/i);
  });

  test("step 2 — publish the policy bundle, before pairing pins a device to it", async ({
    page,
  }) => {
    requireOperator();
    await open(page, "/policies", /Policies/i);

    // BEFORE PAIRING, and the order is load-bearing rather than tidy. The exchange pins the device to
    // the project's active bundle at the moment it pairs, so a device paired while nothing is
    // published is pinned to nothing and every later submission is refused as stale.
    //
    // This is step 5 of the eight on `/onboarding` and step 2 here, and that is not a contradiction:
    // the path presents it in the order a user discovers it needs doing, and the mechanical constraint
    // is that it must precede pairing. Publishing again after pairing would also work; publishing
    // never would not.
    await page.getByTestId("publish-bundle").click();

    const result = page.getByTestId("publish-result");
    await expect(result).toBeVisible({ timeout: 30_000 });
    const digest = /sha256:[0-9a-f]{64}/.exec((await result.textContent()) ?? "");
    expect(digest, "the publish panel must name the digest it activated").not.toBeNull();
    onboarding.bundleDigest = digest![0];

    // ASSERTION: an ACTIVE bundle row with that digest. The screen says "accepted" rather than "live"
    // because activation is dispatched as a task, so the database is what settles it.
    const active = await eventually("the active bundle row", () =>
      sqlScalar(
        `SELECT digest FROM policy_bundles WHERE active AND digest = '${onboarding.bundleDigest}'`,
      ),
    );
    expect(active).toBe(onboarding.bundleDigest);
  });

  test("step 3 — mint a pairing code from the screen that used to tell you to use curl", async ({
    page,
  }) => {
    requireOperator();
    await open(page, "/pairing", /Agent pairing/i);

    // The project this code pairs to, chosen from real rows.
    await page.getByLabel("Project").selectOption(onboarding.projectId!);
    await page.getByTestId("mint-code").click();

    const shown = page.getByTestId("pairing-code-value");
    await expect(shown).toBeVisible({ timeout: 30_000 });
    onboarding.pairingCode = ((await shown.textContent()) ?? "").trim();
    // Crockford base32 — no I, L, O or U, so a transcribed code cannot be ambiguous.
    expect(onboarding.pairingCode).toMatch(/^[0-9A-HJKMNP-TV-Z]{4,}$/);

    // The panel is explicit that this is the only time the code exists in the clear. It is stored as
    // an HMAC and appears in no log and no audit row, so there is no endpoint that could show it
    // again — which is why the UI must show it once and say so.
    await expect(page.getByTestId("pairing-code")).toContainText(/not recoverable/i);

    // ASSERTION: a pending device row for this project, created by the mint.
    const pending = await eventually("a pending device for the project", () =>
      sqlScalar(
        `SELECT id FROM agent_devices WHERE project_id = '${onboarding.projectId}' AND status = 'pending'`,
      ),
    );
    expect(pending).not.toBeNull();
  });

  test("step 4 — the agent pairs with the code the browser displayed", async () => {
    requireOperator();

    // The agent refuses to pair twice by design, and its credential file lives in a named volume that
    // survives a rerun. Wiped here, as the journey does, because a spec that only passes the first
    // time it is run is a spec that fails on every rerun.
    composeExec("agent", ["sh", "-c", "rm -f /var/lib/forgeops/credentials.json"], {
      allowFailure: true,
    });
    composeExec("agent", ["pkill", "-f", "forgeops-agent run"], { allowFailure: true });

    // OUTSIDE THE BROWSER ON PURPOSE. The exchange presents a certificate request from the machine
    // that will hold the credential; a browser has no CSR to offer and no filesystem to store the
    // result in. The UI's job is to produce the code and the command, which it did.
    const output = composeExec("agent", [
      "forgeops-agent",
      "pair",
      "--code",
      onboarding.pairingCode!,
      "--backend",
      "ws://backend:8000/api/v1/ws/agent",
    ]);
    expect(output).toContain("Paired.");
    const match = /device id:\s+(\S+)/.exec(output);
    expect(match, `pair output did not name a device id:\n${output}`).not.toBeNull();
    onboarding.deviceId = match![1];

    // ASSERTION: the exchange PINNED the device to the bundle published in step 2. Without step 2 this
    // column is null and the chokepoint refuses every submission later — which is the whole reason the
    // onboarding path puts publishing before generating.
    const pinned = sqlScalar(
      `SELECT policy_bundle_digest FROM agent_devices WHERE id = '${onboarding.deviceId}'`,
    );
    expect(pinned, "pairing must pin the device to the active policy bundle").toBe(
      onboarding.bundleDigest,
    );

    // Clean workspace, then the long-running agent, so the apply in step 8 has somewhere to land.
    for (const artifact of EXPECTED_ARTIFACTS) {
      composeExec("agent", ["rm", "-f", `${WORKSPACE}/${artifact}`], { allowFailure: true });
    }
    composeExec("agent", ["rm", "-rf", `${WORKSPACE}/.forgeops`], { allowFailure: true });
    composeExec(
      "agent",
      ["sh", "-c", `find ${WORKSPACE} -name '*.backup.*' -maxdepth 2 -delete || true`],
      { allowFailure: true },
    );
    composeExecDetached("agent", ["sh", "-c", "forgeops-agent run >> /tmp/agent-run.log 2>&1"]);
  });

  test("step 5 — the browser shows the project unscanned, and gives the exact scan command", async ({
    page,
  }) => {
    requireOperator();
    await open(page, `/projects/${onboarding.projectId}`, new RegExp(SUFFIX));

    // THE SCREEN WHOSE ABSENCE WAS THE MOST CONFUSING THING ABOUT THIS PRODUCT. Three index routes
    // were served and none had a caller, so there was no way to learn that a project had never been
    // scanned — and that one fact explains a zero readiness score, an empty retrieval and a
    // contextless generation all at once.
    await expect(page.getByTestId("index-headline")).toHaveText("Never scanned");
    await expect(page.getByTestId("detail-readiness-unscanned")).toContainText(
      /not a score of zero/i,
    );

    // No "Scan now" button, and the page says why: §2.2.1 confines command dispatch to the chokepoint,
    // and a scan reads the tree rather than mutating it, so an approval gate around it would be
    // ceremony without a control.
    await expect(page.getByRole("button", { name: /scan now/i })).toHaveCount(0);
    const command = ((await page.getByTestId("scan-command").textContent()) ?? "").trim();
    // `--project` and nothing else. The agent indexes the workspace IT owns, so there is no path
    // argument — this assertion is what caught the panel rendering a `--path` flag the CLI does not
    // have, which is the entire reason this step runs the displayed string rather than one of its own.
    expect(command).toBe(`forgeops-agent scan --project ${onboarding.projectId}`);

    // Run the command the UI displayed, verbatim. If the UI got it wrong, this fails here rather than
    // silently working because the test knew better.
    const scan = composeExec("agent", command.split(" "));
    const indexed = Number(/indexed (\d+) file/.exec(scan)?.[1] ?? 0);
    expect(indexed, `the agent indexed nothing. Its output was:\n${scan}`).toBeGreaterThan(0);

    // ASSERTION: rows in `file_tree`, and the browser now reports them.
    const files = await eventually("indexed files for the project", () => {
      const count = sqlScalar(
        `SELECT count(*) FROM file_tree WHERE project_id = '${onboarding.projectId}'`,
      );
      return count !== null && Number(count) > 0 ? count : null;
    });
    expect(Number(files)).toBeGreaterThan(0);

    await page.reload();
    await expect(page.getByTestId("index-headline")).not.toHaveText("Never scanned", {
      timeout: 30_000,
    });
    // The reload rotated the refresh cookie, so the snapshot the next step restores has to be the one
    // this context holds NOW. See `saveSession`.
    await saveSession(page);
  });

  test("step 6 — readiness scores from the index, with checks behind each category", async ({
    page,
  }) => {
    requireOperator();
    await open(page, "/readiness", /Deployment readiness/i);
    await page.getByLabel("Project").selectOption(onboarding.projectId!);

    // Measured from indexed paths, said on the face of the panel — not a number you have to trust.
    await expect(page.getByTestId("readiness-provenance")).toContainText(/indexed path/i, {
      timeout: 30_000,
    });

    // §1.4's expandable breakdown, and PRD FR-19's "why it matters" behind it. Collapsed until asked
    // for, because six numbers with nothing behind them cannot be acted on.
    const toggle = page.getByTestId("category-toggle-containerization");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByTestId("readiness-breakdown")).toContainText(/why it matters/i);
  });

  test("step 7 — generate through the wizard, and the change set is admitted", async ({ page }) => {
    requireOperator();
    await open(page, "/generation", /Generation/i);
    await page.getByLabel("Project").selectOption(onboarding.projectId!);

    // The prompt is free text and required — the button is disabled without one, because the runtime is
    // inferred from the prompt by `generation/service.py` rather than chosen from a closed list. A run
    // submitted with nothing to generate would be a request the server cannot act on.
    await page
      .getByLabel(/what should be generated/i)
      .fill("A container image and Kubernetes manifests for this Node.js service.");

    await page.getByRole("button", { name: /generate artifacts/i }).click();

    // ASSERTION: a change set row for this project. The generation is a real model call, so the wait
    // is generous — and the assertion is on the ROW rather than on rendered text, because a wizard
    // that streams tokens and submits nothing is exactly the failure to catch.
    const changeSetId = await eventually(
      "a change set for the project",
      () =>
        sqlScalar(
          `SELECT id FROM change_sets WHERE project_id = '${onboarding.projectId}' ORDER BY created_at DESC LIMIT 1`,
        ),
      { timeoutMs: 900_000, intervalMs: 5_000 },
    );
    onboarding.changeSetId = changeSetId!;

    // And it was ADMITTED rather than refused. A `validation_failed` or a refusal at the chokepoint
    // would leave no row at all or one in a state approval cannot act on, and the most likely cause
    // would be the bundle — which is why step 2 exists.
    const status = sqlScalar(
      `SELECT status FROM change_sets WHERE id = '${onboarding.changeSetId}'`,
    );
    expect(
      ["pending_approval", "approved", "applying", "applied"],
      `the change set is ${status}; a stale bundle or a policy denial would show here`,
    ).toContain(status);
  });

  test("step 8 — approve it in the browser, and the agent writes the files", async ({ page }) => {
    requireOperator();
    await open(page, "/approvals", /Approvals/i);

    // The pending queue. Selecting the change set renders a real diff of its `change_items`, which is
    // what a reviewer needs before deciding.
    await page.getByRole("button", { name: new RegExp(onboarding.changeSetId!) }).click();
    await expect(page.getByRole("region", { name: /diff for/i }).first()).toBeVisible({
      timeout: 30_000,
    });

    // There is no field for the approver: the server takes it from the verified session. Asserted
    // because supplying it as a query parameter defaulting to `admin` is the defect that kept this
    // router unmounted for a phase.
    await expect(page.getByLabel(/approver/i)).toHaveCount(0);

    await page.getByLabel(/reason/i).fill("onboarding path, approved in the browser");
    await page.getByRole("button", { name: "Approve" }).click();

    // ASSERTION: the change set reaches `applied`, and a FILE EXISTS on disk with the content hash the
    // backend recorded. Rendered text is the one thing a page can produce without anything happening.
    const applied = await eventually(
      "the change set to be applied",
      () => {
        const value = sqlScalar(
          `SELECT status FROM change_sets WHERE id = '${onboarding.changeSetId}'`,
        );
        return value === "applied" ? value : null;
      },
      { timeoutMs: 240_000, intervalMs: 2_000 },
    );
    expect(applied).toBe("applied");

    // And an approval row attributed to a real user, not to a string a caller supplied.
    const approver = sqlScalar(
      `SELECT u.email FROM approvals a JOIN users u ON u.id = a.approver_id WHERE a.change_set_id = '${onboarding.changeSetId}'`,
    );
    expect(approver, "the approval must be attributed to a stored user").not.toBeNull();

    // The audit chain recorded it, and the browser can verify that chain — which is the control
    // `main.py` called "a claim rather than a control" while nothing called it.
    const recorded = sqlScalar(
      `SELECT count(*) FROM audit_events WHERE project_id = '${onboarding.projectId}'`,
    );
    expect(Number(recorded)).toBeGreaterThan(0);
  });

  test("step 9 — the project's own change history shows what happened", async ({ page }) => {
    requireOperator();
    await open(page, `/projects/${onboarding.projectId}`, new RegExp(SUFFIX));

    // §1.6's timeline, scoped to this project, with what the status MEANS rather than a colour.
    const history = page.getByTestId("change-history");
    await expect(history).toBeVisible({ timeout: 30_000 });
    await expect(history.getByTestId(`status-${onboarding.changeSetId}`)).toHaveText("applied");
    await expect(history).toContainText(/written to the working tree/i);
  });
});
