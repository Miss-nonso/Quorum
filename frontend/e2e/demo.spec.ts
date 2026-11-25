import { expect, test, type Page } from "@playwright/test";

import { expectAccessible } from "./a11y";

/**
 * The public demo, driven exactly as a visitor would. Each test gets a fresh
 * browser context, so a fresh sandbox. The backend is real: real workers are
 * really killed (they exit and are respawned as new workers).
 */

async function skipGuide(page: Page) {
  await page.addInitScript(() => localStorage.setItem("conductor-guide-done", "1"));
}

const runBadge = (page: Page) => page.getByText(/^Run [0-9a-f]{8}/).locator("..");

test("the guide walks through four steps and can be dismissed", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Kill a worker");
  const guide = page.getByRole("region", { name: /Guide, step 1 of 4/ });
  await expect(guide).toContainText("This is a workflow");
  await expectAccessible(page);
  await guide.getByRole("button", { name: "Next tip" }).click();
  await expect(page.getByRole("region", { name: /Guide, step 2 of 4/ })).toContainText("Run it");
  await page.getByRole("button", { name: "Skip the tour" }).click();
  await expect(page.getByRole("region", { name: /Guide, step/ })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: /Guide, step/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Show the guide" }).click();
  await expect(page.getByRole("region", { name: /Guide, step 1 of 4/ })).toBeVisible();
});

test("kill a worker mid-run: its task is recovered and the run succeeds", async ({ page }) => {
  await skipGuide(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Run the pipeline" }).click();

  // Wait until a step is actually running on some worker, then crash that worker.
  await expect(page.getByRole("button", { name: /Running/ }).first()).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: "Kill a worker" }).click();
  await expect(page.getByRole("status").filter({ hasText: /Crashing (idle )?worker/ })).toBeVisible();

  const story = page.getByRole("region", { name: "Story of your runs" });
  await expect(story).toContainText("The demo crashed worker", { timeout: 15_000 });
  await expect(story).toContainText("went silent", { timeout: 30_000 });
  await expect(runBadge(page).getByText("Succeeded")).toBeVisible({ timeout: 60_000 });
  await expectAccessible(page);
});

test("inject failures: retries with backoff, dead-lettering, then one-click replay", async ({ page }) => {
  await skipGuide(page);
  await page.goto("/");
  const toggle = page.getByRole("switch", { name: "Inject failures" });
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await page.getByRole("button", { name: "Run the pipeline" }).click();

  const story = page.getByRole("region", { name: "Story of your runs" });
  await expect(story).toContainText("retrying in", { timeout: 30_000 });
  const alert = page.getByRole("alert").filter({ hasText: "dead-letter queue" });
  await expect(alert).toBeVisible({ timeout: 60_000 });
  await expect(runBadge(page).getByText("Failed")).toBeVisible();
  await expect(page.getByRole("button", { name: /^transform\b.*attempt 3\/3/ })).toBeVisible();

  await alert.getByRole("button", { name: "Replay notify" }).click();
  await expect(runBadge(page).getByText("Succeeded")).toBeVisible({ timeout: 60_000 });
  await expectAccessible(page);
});

test("flood the queue: urgent jobs jump ahead and nothing starves", async ({ page }) => {
  await skipGuide(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Flood the queue" }).click();
  const high = page.getByRole("progressbar", { name: "High priority" });
  const low = page.getByRole("progressbar", { name: "Low priority" });
  await expect(high).toBeVisible();
  await expect(high).toHaveAttribute("aria-valuenow", (await high.getAttribute("aria-valuemax")) ?? "", {
    timeout: 60_000,
  });
  await expect(low).toHaveAttribute("aria-valuenow", (await low.getAttribute("aria-valuemax")) ?? "", {
    timeout: 60_000,
  });
});
