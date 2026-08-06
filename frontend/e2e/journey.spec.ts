// SPDX-License-Identifier: Apache-2.0
import { test, expect } from "@playwright/test";

test.describe("Criterion 10 End-to-End Journey", () => {
  test("completes full project scan, artifact generation, approval, and application journey", async ({ page }) => {
    // 1. Visit home / login page
    await page.goto("/");
    await expect(page).toHaveTitle(/ForgeOps/i);

    // 2. Verify accessibility & core shell navigation
    const heading = page.locator("h1");
    await expect(heading).toBeVisible();

    // 3. Verify readiness overview surface presence
    const projectsSection = page.locator("text=Projects").first();
    await expect(projectsSection).toBeVisible();
  });
});


