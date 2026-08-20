import { test, expect } from "@playwright/test";

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

  test("the three unserved features state that they are not implemented", async ({ page }) => {
    for (const path of ["/approvals", "/generation", "/pairing"]) {
      await page.goto(path);
      await expect(
        page.getByRole("heading", { name: /is not implemented in Phase 1/ }),
      ).toBeVisible();
      // The reason must be specific, not a shrug.
      await expect(page.getByText("Why this screen is empty")).toBeVisible();
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
    // Force the API to fail. A fixture fallback would show content anyway; the contract is that
    // it must not.
    await page.route("**/api/v1/**", (route) => route.abort());
    await page.goto("/audit");
    await expect(page.getByText(/Could not load|Sign-in required/)).toBeVisible();
    await expect(page.getByText("USER_APPROVE")).toHaveCount(0);
  });
});
