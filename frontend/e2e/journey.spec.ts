// SPDX-License-Identifier: Apache-2.0
//
// A SHELL SMOKE TEST. Not the criterion-10 journey, despite what this file was cited as.
//
// Relabelled 2026-08-21 (D-94). PROGRESS.md recorded criterion 10 as met on the strength of this
// file, claiming it "runs the 13-step journey against built backend and frontend images with a
// real paired agent container and a fixture Node.js project, ending in on-disk assertions and a
// byte-exact revert". It does none of that and never did. It loads one page and makes three
// assertions. The workflow that runs it builds only the frontend.
//
// KEPT RATHER THAN DELETED, deliberately, for two reasons. It is a genuine smoke test — if the
// shell stops rendering, this goes red — and it is the seam the real journey grows along, so
// deleting it would mean starting the 13 steps from an empty file later.
//
// WHAT THE REAL JOURNEY NEEDS, from design §12.6, scoped against the tree in D-94. The spine is
// blocked on surfaces that do not exist rather than on test-writing effort:
//
//   step 6  generate      backend/src/generation/ has a service, schemas and models but no
//                         routes.py, so there is no endpoint to call
//   step 7  SSE stream    the transport is real and tested; nothing produces events without 6
//   steps 8-9  approve    the approvals router is unmounted AND unmountable: no route requires a
//                         principal, and the approver is a query parameter defaulting to "admin"
//   steps 10,11,13        on-disk assertions, backups and the byte-exact revert all depend on an
//                         apply that cannot happen without 6 and 9
//   step 1  login         no fixture OIDC issuer service exists (design §8.3.2); no sign-in screen
//   step 2  create        POST /projects echoes its input; nothing is persisted
//   step 4  device state   the device routes are two POSTs and a DELETE — there is no GET
//
// Steps 5 and 12 are the nearest to reachable and still partial: readiness serves a real score but
// not the category breakdown step 5 names, and the audit viewer is real but no transit can exist
// for it to list. So: 0 of 13 today.
//
// When those surfaces land, extend THIS file step by step, and hold to the rule the old version
// broke — every step must assert on something observable: an HTTP status, a row in the database,
// a file on disk. A step that asserts only on rendered text is how "the UI says applied" passed
// for a journey that never applied anything.

import { test, expect } from "@playwright/test";

test.describe("Shell smoke (criterion 10 is descoped to this by D-94)", () => {
  test("the shell renders, titled, with a single top-level heading", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/ForgeOps/i);

    const heading = page.locator("h1");
    await expect(heading).toBeVisible();
    // Exactly one, so a route that grows a second h1 is caught rather than tolerated.
    await expect(heading).toHaveCount(1);
  });

  test("the primary navigation reaches the projects route", async ({ page }) => {
    await page.goto("/");
    // This asserted `text=Projects` was visible, which the old single-page dashboard satisfied
    // with a hardcoded heading. It now follows the link and checks the route resolves, so the
    // assertion is about navigation rather than about a string existing somewhere on a page.
    const projects = page.getByRole("link", { name: "Projects", exact: true });
    await expect(projects).toBeVisible();
    await projects.click();
    await expect(page).toHaveURL("/projects");
    await expect(page.locator("h1")).toHaveCount(1);
  });
});
