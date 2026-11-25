import { expect, test } from "@playwright/test";

import { expectAccessible } from "./a11y";

test.describe("dashboard smoke @mobile", () => {
  for (const theme of ["light", "dark"] as const) {
    test(`overview is live and accessible (${theme})`, async ({ page }) => {
      await page.addInitScript((t) => localStorage.setItem("quorum-theme", t), theme);
      await page.goto("/overview");
      await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
      await expect(page.getByRole("status").filter({ hasText: "Live" })).toBeVisible();
      await expect(page.getByText("Succeeded").first()).toBeVisible();
      // Nothing may force sideways scrolling, least of all on a phone.
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, "horizontal overflow in px").toBeLessThanOrEqual(0);
      await expectAccessible(page);
    });
  }
});

test("trigger the sample workflow and watch it finish", async ({ page }) => {
  await page.goto("/workflows");
  await page.getByRole("link", { name: "order-pipeline" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "order-pipeline" })).toBeVisible();
  await page.getByRole("button", { name: /Run now/ }).click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  // The status badge in the heading flips to Succeeded via the live stream.
  await expect(page.getByRole("heading", { level: 1 }).getByText("Succeeded")).toBeVisible({
    timeout: 30_000,
  });
  for (const step of ["fetch", "validate", "transform", "store", "report", "notify"]) {
    await expect(page.getByRole("button", { name: new RegExp(`^${step}\\b.*Succeeded`) })).toBeVisible();
  }

  // Inspect a step: the panel shows its result.
  await page.getByRole("button", { name: /^store\b/ }).click();
  await expect(page.getByRole("heading", { name: "store" })).toBeVisible();
  await expect(page.getByLabel("Result of store")).toContainText('"stored": 4');
  await expect(page.getByRole("img", { name: /Execution timeline/ })).toBeVisible();
  await expectAccessible(page);
});

test("history, workers and dead letters render and are accessible", async ({ page }) => {
  await page.goto("/runs");
  await expect(page.getByRole("table")).toBeVisible();
  await page.getByLabel("Status").selectOption("succeeded");
  await expect(page).toHaveURL(/status=succeeded/);
  await expectAccessible(page);

  await page.goto("/workers");
  await expect(page.getByRole("progressbar").first()).toBeVisible();
  await expectAccessible(page);

  await page.goto("/dead-letters");
  await expect(page.getByRole("heading", { level: 1, name: "Dead-letter queue" })).toBeVisible();
  await expectAccessible(page);
});

test("keyboard users can skip to content and reach every nav link", async ({ page }) => {
  await page.goto("/overview");
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();
  for (const name of ["Demo", "Overview", "Workflows", "History", "Workers", "Dead letters"]) {
    await expect(
      page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: new RegExp(name) }),
    ).toBeVisible();
  }
});
