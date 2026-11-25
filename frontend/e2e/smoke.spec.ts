import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const summary = results.violations.map(
    (v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`,
  );
  expect(summary, "axe violations").toEqual([]);
}

test.describe("dashboard smoke @mobile", () => {
  for (const theme of ["light", "dark"] as const) {
    test(`overview is live and accessible (${theme})`, async ({ page }) => {
      await page.addInitScript((t) => localStorage.setItem("conductor-theme", t), theme);
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
      await expect(page.getByRole("status").filter({ hasText: "Live" })).toBeVisible();
      await expect(page.getByText("Succeeded").first()).toBeVisible();
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
    await expect(page.getByRole("button", { name: new RegExp(`^${step}, .*Succeeded`) })).toBeVisible();
  }

  // Inspect a step: the panel shows its result.
  await page.getByRole("button", { name: /^store,/ }).click();
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

test("a dead-lettered job can be replayed with one click", async ({ page, request }) => {
  const job = await request.post(`${API}/api/v1/jobs`, {
    data: { task_type: "flaky", params: { failure_rate: 1 }, max_attempts: 1 },
  });
  expect(job.ok()).toBeTruthy();
  const { task_id } = await job.json();

  await page.goto("/dead-letters");
  const card = page.getByRole("listitem").filter({ hasText: "simulated failure on attempt 1" }).first();
  await expect(card).toBeVisible({ timeout: 20_000 });
  await card.getByRole("button", { name: /Replay/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "back in the queue" })).toBeVisible();

  const task = await (await request.get(`${API}/api/v1/dead-letters?include_replayed=true&limit=100`)).json();
  expect(
    task.items.some(
      (l: { task_id: string; replayed_at: string | null }) => l.task_id === task_id && l.replayed_at,
    ),
  ).toBe(true);
});

test("keyboard users can skip to content and reach every nav link", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();
  for (const name of ["Workflows", "History", "Workers", "Dead letters"]) {
    await expect(
      page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: new RegExp(name) }),
    ).toBeVisible();
  }
});
