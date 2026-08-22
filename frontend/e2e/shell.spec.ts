import { test, expect } from "@playwright/test";

/**
 * WHY THIS FILE FULFILS ONE REQUEST, AND EXACTLY ONE
 *
 * These tests assert the authenticated shell: the sidebar, its nine entries, the skip link, the
 * theme toggle, and one `h1` per route. Reaching that shell needs a session, and this job runs with
 * no backend at all -- that is the point of it, it is the fast gate.
 *
 * `AuthBoundary` recovers a session by calling `POST /auth/refresh`, because the access token lives
 * in memory and only the httpOnly refresh cookie survives a reload. With nothing listening, that
 * call fails, the boundary settles on "anonymous", and it redirects to `/login`. So every assertion
 * below was racing a redirect: the DOM captured at failure was the sign-in screen, and the handful
 * of tests that passed passed because a fast assertion beat the redirect rather than because the
 * shell was correct. Sixteen failures, three retries each, all of them this.
 *
 * So the refresh call -- and NOTHING else -- is fulfilled here. That is a test double for the one
 * dependency these tests are not about; authentication itself is proved elsewhere, by the journey
 * against a real Authentik and by tests/integration/test_authentik_real_idp.py against a real IdP.
 *
 * What is deliberately NOT stubbed: every product endpoint. `/projects`, `/audit`, `/readiness` and
 * the rest still hit an absent backend and still fail, because the second half of this file exists
 * to prove that an unreachable API produces a Problem Details panel and never a fabricated fixture.
 * Stubbing those would delete the assertion while appearing to strengthen it.
 */
test.beforeEach(async ({ page }) => {
  await page.route("**/auth/refresh", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        access_token: "smoke-only-not-a-real-token",
        subject: "smoke-only-not-a-real-subject",
        session_id: null,
      }),
    });
  });
});

test.describe("Shell layout", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
  });

  test("loads the homepage at localhost", async ({ page }) => {
    await expect(page).toHaveTitle(/ForgeOps/);
  });

  test("skip-link is present and receives focus on Tab", async ({ page }) => {
    const skipLink = page.locator('a[href="#main"]');
    await expect(skipLink).toBeAttached();
    await page.keyboard.press("Tab");
    await expect(skipLink).toBeFocused();
    await expect(skipLink).toHaveText("Skip to main content");
  });

  test("Home link is keyboard activatable and has aria-current=page at /", async ({ page }) => {
    const homeLink = page.getByRole("link", { name: "Home" });
    await expect(homeLink).toBeVisible();
    await expect(homeLink).toHaveAttribute("aria-current", "page");
    await expect(homeLink).toHaveAttribute("href", "/");
    // Focus and activate via keyboard
    await homeLink.focus();
    await expect(homeLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL("/");
  });

  test("active styling is applied to Home at /", async ({ page }) => {
    const homeLink = page.getByRole("link", { name: "Home" });
    await expect(homeLink).toHaveClass(/bg-sidebar-accent/);
  });

  test("landmarks are correct: nav[Primary], main[#main]", async ({ page }) => {
    const nav = page.getByRole("navigation", { name: "Primary" });
    await expect(nav).toBeVisible();
    const main = page.locator("main#main");
    await expect(main).toBeVisible();
  });

  test("has exactly one h1 heading", async ({ page }) => {
    const headings = page.locator("h1");
    await expect(headings).toHaveCount(1);
  });

  test("theme toggle persists theme preference", async ({ page }) => {
    const themeToggle = page.getByRole("button", { name: /theme/i });
    await expect(themeToggle).toBeVisible();

    // Click to change theme
    await themeToggle.click();

    // Verify the html element has dark class
    const htmlClass = await page.locator("html").getAttribute("class");
    const isDark = htmlClass?.includes("dark");

    // Reload and check persistence
    await page.reload();
    const htmlClassAfterReload = await page.locator("html").getAttribute("class");
    const isDarkAfterReload = htmlClassAfterReload?.includes("dark");
    expect(isDarkAfterReload).toBe(isDark);
  });

  test("no placeholder or disabled navigation items", async ({ page }) => {
    const nav = page.getByRole("navigation", { name: "Primary" });
    // Nine routes: Home plus the eight feature modules, each of which now has a page. This
    // asserted exactly 1 while the sidebar had a single Home link and the other eight modules
    // were mounted on nothing. The count is deliberately exact rather than `>= 1`, so adding a
    // nav entry without a route to back it fails here.
    const links = nav.locator("a");
    await expect(links).toHaveCount(9);
    // No disabled elements
    const disabled = nav.locator('[aria-disabled="true"], [disabled]');
    await expect(disabled).toHaveCount(0);
    // No "Coming Soon" or placeholder text
    const navText = await nav.textContent();
    expect(navText).not.toContain("Coming Soon");
    expect(navText).not.toContain("Disabled");
    expect(navText).not.toContain("placeholder");
  });
});

/**
 * The routes, and the honesty rule they exist to enforce.
 *
 * Every sidebar entry must reach a real page with exactly one `h1`, and no page may render
 * fabricated data. The second half is what these tests are really for: the dashboard used to
 * render a hardcoded project with a readiness score of 95 that no backend had computed, and three
 * feature modules shipped invented content. So `sampleProjects` must not come back, and the
 * feature surfaces with no endpoint must keep saying so.
 *
 * These assertions hold with the backend absent, which is the point — an unreachable API produces
 * a Problem Details panel, never a fallback fixture.
 */
const ROUTES = [
  { path: "/", nav: "Home" },
  { path: "/projects", nav: "Projects" },
  { path: "/readiness", nav: "Readiness" },
  { path: "/audit", nav: "Audit" },
  { path: "/policies", nav: "Policies" },
  { path: "/vault", nav: "Vault" },
  { path: "/approvals", nav: "Approvals" },
  { path: "/generation", nav: "Generation" },
  { path: "/pairing", nav: "Pairing" },
] as const;

test.describe("Feature routes", () => {
  for (const { path, nav } of ROUTES) {
    test(`${path} renders, has one h1, and marks its nav item current`, async ({ page }) => {
      const response = await page.goto(path);
      expect(response?.status()).toBeLessThan(400);

      await expect(page.locator("h1")).toHaveCount(1);

      const link = page.getByRole("link", { name: nav, exact: true });
      await expect(link).toHaveAttribute("aria-current", "page");
    });
  }

  test("every sidebar entry navigates to a live route", async ({ page }) => {
    await page.goto("/");
    for (const { path, nav } of ROUTES.slice(1)) {
      await page.getByRole("link", { name: nav, exact: true }).click();
      await expect(page).toHaveURL(path);
      await expect(page.locator("h1")).toHaveCount(1);
    }
  });

  test("no route renders the not-implemented panel", async ({ page }) => {
    // This test used to assert the OPPOSITE for /approvals, /generation and /pairing: that each
    // stated it was not implemented in Phase 1. That was true when it was written and is not any
    // more -- approvals reads GET /api/v1/approvals, generation streams the six SSE event types from
    // POST /api/v1/generation/runs, and pairing queries the device list. `NotImplemented` is no
    // longer imported by any file under app/, so the old assertion could only ever fail.
    //
    // Inverted rather than deleted, because the honesty rule it enforced still matters: the panel
    // exists so an unserved surface says so instead of showing sample data, and this now fails the
    // moment a route regresses to a placeholder. Every one of the nine is checked, not just three.
    for (const { path } of ROUTES) {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: /is not implemented in Phase 1/ })).toHaveCount(
        0,
      );
      await expect(page.getByText("Why this screen is empty")).toHaveCount(0);
      // And it is a real surface rather than a blank one.
      await expect(page.locator("h1")).toHaveCount(1);
    }
  });

  test("the dashboard no longer renders the hardcoded sample project", async ({ page }) => {
    await page.goto("/");
    // The exact fixture that used to be here: one project named "ForgeOps Platform" with a
    // readinessScore of 95, both invented. `exact` matters on the number: a substring match for
    // "95" is satisfied by any prose or commit hash containing those digits, which is how this
    // assertion first passed for the wrong reason.
    await expect(page.getByText("ForgeOps Platform")).toHaveCount(0);
    await expect(page.getByText("95", { exact: true })).toHaveCount(0);
  });

  test("live panels surface a Problem Details envelope rather than falling back to fixtures", async ({
    page,
  }) => {
    // Force the PRODUCT API to fail. A fixture fallback would show content anyway; the contract is
    // that it must not.
    //
    // `/auth/` is excluded, and that exclusion is what makes the test measure what it claims. A blanket
    // abort of `**/api/v1/**` also kills `POST /auth/refresh`, so `AuthBoundary` settles on anonymous
    // and redirects to the sign-in screen -- and the assertion then fails because there is no panel on
    // that screen, not because a fixture leaked. `route.fallback()` hands those requests to the
    // handler registered at the top of this file instead.
    await page.route("**/api/v1/**", (route) =>
      route.request().url().includes("/auth/") ? route.fallback() : route.abort(),
    );
    await page.goto("/audit");
    await expect(page.getByText(/Could not load|Sign-in required/)).toBeVisible();
    await expect(page.getByText("USER_APPROVE")).toHaveCount(0);
  });
});
