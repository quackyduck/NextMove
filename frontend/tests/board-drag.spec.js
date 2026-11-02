import { test, expect } from "@playwright/test";

const workspacePath = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;

async function signIn(page) {
  // Wide enough that the target columns are on screen, so the drag needs no scrolling.
  await page.setViewportSize({ width: 2700, height: 900 });
  await page.goto(`${workspacePath}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
}

// Native drag and drop needs gradual mouse moves; a single jump never starts the drag.
async function drag(page, source, target) {
  const a = await source.boundingBox(), t = await target.boundingBox();
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move(a.x + a.width / 2 + 10, a.y + a.height / 2, { steps: 5 });
  await page.mouse.move(t.x + t.width / 2, t.y + 60, { steps: 10 });
  await page.mouse.up();
}

const column = (page, name) =>
  page.locator(".board-column").filter({ has: page.getByRole("heading", { name }) });

// Run against a disposable backend database. This test creates a job.
test("dragging a board card to another column saves its stage", async ({ page }) => {
  const title = `Drag target ${Date.now()}`;
  await signIn(page);
  await page.getByRole("button", { name: "Capture a role", exact: false }).first().click();
  await page.getByLabel("Role title", { exact: true }).fill(title);
  await page.getByLabel("Company", { exact: true }).fill("Drag Labs");
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page.getByRole("button", { name: "Your pipeline", exact: false }).first().click();
  await page.getByLabel("Search roles", { exact: true }).fill(title);
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await expect(column(page, /^Interesting/)).toContainText(title);

  await drag(page, page.getByRole("button", { name: title }), column(page, /^Applied/));
  await expect(page.getByRole("status")).toContainText(`Moved “${title}” to Applied`);
  await expect(column(page, /^Applied/)).toContainText(title);
  await expect(column(page, /^Interesting/)).not.toContainText(title);

  // The move persisted.
  await page.reload();
  await page.getByLabel("Search roles", { exact: true }).fill(title);
  await expect(page.locator("tbody")).toContainText("Applied");
});

test("a failed move puts the card back and explains why", async ({ page }) => {
  const title = `Drag conflict ${Date.now()}`;
  await signIn(page);
  await page.getByRole("button", { name: "Capture a role", exact: false }).first().click();
  await page.getByLabel("Role title", { exact: true }).fill(title);
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page.getByRole("button", { name: "Your pipeline", exact: false }).first().click();
  await page.getByLabel("Search roles", { exact: true }).fill(title);
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await page.route("**/api/v1/jobs/*", async (route) => {
    if (route.request().method() === "PATCH")
      await route.fulfill({ status: 409, contentType: "application/json",
        body: JSON.stringify({ error: "This job changed. Reload it before saving." }) });
    else await route.continue();
  });
  await drag(page, page.getByRole("button", { name: title }), column(page, /^Resume updated/));
  await expect(page.getByRole("alert")).toContainText("changed elsewhere");
  await expect(column(page, /^Interesting/)).toContainText(title);
  await expect(column(page, /^Resume updated/)).not.toContainText(title);
});
