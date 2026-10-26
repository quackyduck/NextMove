import { test, expect } from "@playwright/test";

const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${path}api/v1`;

test("Not Interested persists independently of stage and filters table and board", async ({ page }) => {
  const title = "Opportunity I am not interested in";
  await page.goto(path);
  await page.getByLabel("Workspace password", { exact: true }).fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(page.getByRole("heading", { name: "Your pipeline.", exact: true })).toBeVisible();
  const session = await (await page.request.get(`${api}/auth/session`)).json();
  const saved = await page.request.post(`${api}/jobs`, {
    headers: { "X-CSRF-Token": session.csrf_token },
    data: { title, stage: "applied" },
  });
  expect(saved.status()).toBe(201);
  await page.reload();
  await page.getByRole("button", { name: title, exact: false }).click();
  const outcome = page.getByLabel("Outcome", { exact: true });
  await expect(outcome.getByRole("option", { name: "Not Interested", exact: true })).toHaveAttribute("value", "not_interested");
  await outcome.selectOption("not_interested");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  await expect(page.getByLabel("Stage", { exact: true })).toHaveValue("applied");
  await page.getByRole("button", { name: "Your pipeline", exact: false }).first().click();
  await expect(page.getByRole("button", { name: title, exact: false })).toHaveCount(0);
  await page.getByLabel("Filter by outcome").selectOption("not_interested");
  await expect(page.locator("tbody")).toContainText("Not Interested");
  await expect(page.getByRole("button", { name: title, exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await expect(page.locator(".board-column").filter({ has: page.getByRole("heading", { name: /Applied/ }) })).toContainText(title);
  await page.reload();
  await page.getByLabel("Filter by outcome").selectOption("not_interested");
  await page.getByRole("button", { name: title, exact: false }).click();
  await expect(page.getByLabel("Outcome", { exact: true })).toHaveValue("not_interested");
  await expect(page.getByLabel("Stage", { exact: true })).toHaveValue("applied");
});
