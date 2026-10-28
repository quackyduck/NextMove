import { test, expect } from "@playwright/test";
const workspacePath = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;

async function openQuick(page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(workspacePath);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Workspace password").fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByRole("button", { name: "Quick capture", exact: false }).click();
  await expect(page.getByRole("heading", { name: "Quick capture." })).toBeVisible();
}

const fields = {
  title: "Mobile import engineer", company: "Acme", url: "https://example.com/role",
  salary: "USD 150000–190000 / year", notes: "Key qualifications\nPython and SQL",
  description: "Build reliable systems.",
};

test("phone URL preview, review edits, save once and persist real job", async ({ page }, testInfo) => {
  await openQuick(page);
  let extractionBody;
  await page.route("**/api/v1/jobs/extract", async route => {
    extractionBody = route.request().postDataJSON();
    await route.fulfill({ status: 202, json: { id: "preview-one", status: "running" } });
  });
  await page.route("**/api/v1/extractions/preview-one", route => route.fulfill({ json: { id: "preview-one", status: "succeeded", fields, warnings: [] } }));
  await page.getByLabel("Posting URL", { exact: true }).fill(fields.url);
  await page.getByRole("button", { name: "Extract role details" }).click();
  await expect(page.getByLabel("Company", { exact: true })).toHaveValue("Acme");
  expect(extractionBody).toEqual({ url: fields.url });
  await expect(page.getByLabel("Notes / key qualifications")).toHaveValue(fields.notes);
  await expect(page.getByLabel("Salary / compensation")).toHaveValue(fields.salary);
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await page.screenshot({ path: testInfo.outputPath("mobile-preview.png"), fullPage: true });
  await page.getByLabel("Company", { exact: true }).fill("Reviewed Acme");
  await page.getByRole("button", { name: "Your pipeline", exact: false }).first().click();
  await expect(page.getByRole("button", { name: fields.title, exact: false })).toHaveCount(0);
  await page.getByRole("button", { name: "Quick capture", exact: false }).click();
  await expect(page.getByLabel("Company", { exact: true })).toHaveValue("Reviewed Acme");
  let saves = 0;
  page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/api/v1/jobs")) saves++; });
  await page.getByRole("button", { name: "Save potential role" }).click();
  await expect(page.getByRole("heading", { name: fields.title })).toBeVisible();
  expect(saves).toBe(1);
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue(fields.notes);
  await page.reload();
  await page.getByRole("button", { name: fields.title, exact: false }).click();
  await expect(page.getByLabel("Company", { exact: true })).toHaveValue("Reviewed Acme");
  await expect(page.getByLabel("Salary / compensation")).toHaveValue(fields.salary);
});

test("failed extraction retains URL for retry and manual fallback", async ({ page }) => {
  await openQuick(page);
  await page.route("**/api/v1/jobs/extract", async route => {
    await route.fulfill({ status: 422, json: { error: "The site blocks automated access. Enter details manually." } });
  });
  await page.getByLabel("Posting URL", { exact: true }).fill(fields.url);
  await page.getByRole("button", { name: "Extract role details" }).click();
  await expect(page.getByRole("alert")).toContainText("blocks automated access");
  await expect(page.getByLabel("Posting URL", { exact: true })).toHaveValue(fields.url);
  await page.getByRole("button", { name: "Enter details manually" }).click();
  await expect(page.getByLabel("Job posting URL")).toHaveValue(fields.url);
});

test("partial preview and failed save preserve edits", async ({ page }) => {
  await openQuick(page);
  await page.route("**/api/v1/jobs/extract", async route => {
    await route.fulfill({ status: 202, json: { id: "partial-preview", status: "running" } });
  });
  await page.getByLabel("Posting URL", { exact: true }).fill(fields.url);
  await page.route("**/api/v1/extractions/partial-preview", route => route.fulfill({ json: { id: "partial-preview", status: "succeeded", fields: { ...fields, salary: "", notes: "" }, warnings: ["Not found on the page: salary, key qualifications."] } }));
  await page.getByRole("button", { name: "Extract role details" }).click();
  await expect(page.getByRole("status")).toContainText("Not found");
  await page.getByLabel("Notes / key qualifications").fill("My review notes");
  await page.route("**/api/v1/jobs", async route => {
    if (route.request().method() === "POST") await route.fulfill({ status: 503, json: { error: "Temporary save failure" } });
    else await route.continue();
  });
  await page.getByRole("button", { name: "Save potential role" }).click();
  await expect(page.getByRole("alert")).toContainText("Temporary save failure");
  await expect(page.getByLabel("Notes / key qualifications")).toHaveValue("My review notes");
  await page.getByRole("button", { name: "Use another URL" }).click();
  await expect(page.getByLabel("Posting URL", { exact: true })).toHaveValue(fields.url);
});

test("background polling keeps navigation usable and resumes after sign-in", async ({ page }) => {
  await openQuick(page);
  await page.route("**/api/v1/jobs/extract", route => route.fulfill({ status: 202, json: { id: "slow-preview", status: "running" } }));
  let state = "running";
  await page.route("**/api/v1/extractions/slow-preview", route => {
    if (state === "unavailable") return route.fulfill({ status: 503, json: { error: "Temporary status failure" } });
    if (state === "expired") return route.fulfill({ status: 401, json: { error: "Authentication required" } });
    return route.fulfill({ json: { id: "slow-preview", status: state, ...(state === "succeeded" ? { fields, warnings: [] } : {}) } });
  });
  await page.getByLabel("Posting URL", { exact: true }).fill(fields.url);
  await page.getByRole("button", { name: "Extract role details" }).click();
  await expect(page.getByRole("status")).toContainText("background");
  await page.getByRole("button", { name: "Your pipeline", exact: false }).first().click();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
  await page.getByRole("button", { name: "Quick capture", exact: false }).click();
  state = "unavailable";
  await expect(page.getByRole("alert")).toContainText("Temporary status failure");
  state = "running";
  await page.getByRole("button", { name: "Check extraction status" }).click();
  await expect(page.getByRole("status")).toContainText("background");
  state = "expired";
  await expect(page.getByRole("dialog")).toBeVisible();
  state = "succeeded";
  await page.getByRole("dialog").getByLabel("Workspace password").fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("dialog").getByRole("button", { name: "Sign back in" }).click();
  await expect(page.getByLabel("Company", { exact: true })).toHaveValue("Acme");
});
