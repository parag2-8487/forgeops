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
    // Only one link should exist
    const links = nav.locator("a");
    await expect(links).toHaveCount(1);
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
