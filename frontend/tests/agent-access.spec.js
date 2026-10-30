import { test, expect } from "@playwright/test";
const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${path}api/v1`;
const password = process.env.E2E_PASSWORD || "local-demo-only";

async function openAccess(page) {
  await page.goto(`${path}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
  await page.getByRole("button", { name: "Agent access", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent access." })).toBeVisible();
}

async function fillCreate(page, name) {
  await page.getByLabel("Token name", { exact: true }).fill(name);
  await page.getByLabel("Current workspace password", { exact: true }).fill(password);
}

test("mobile token creation, one-time secret, connection checks and revocation", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
    value: { writeText: async value => { window.copiedToken = value; } },
  }));
  await openAccess(page);
  await fillCreate(page, "Phone agent");
  await page.getByRole("button", { name: "Create token", exact: true }).click();
  const secret = await page.getByLabel("New token", { exact: true }).inputValue();
  expect(secret).toMatch(/^nm_ingest_/);
  const listed = await (await page.request.get(`${api}/agent-tokens`)).json();
  expect(JSON.stringify(listed)).not.toContain(secret);
  expect(JSON.stringify(listed)).not.toContain("token_hash");
  await expect(page.getByRole("region", { name: "New token setup" })).toContainText("shown once");
  await page.getByRole("button", { name: "Copy token", exact: true }).click();
  expect(await page.evaluate(() => window.copiedToken)).toBe(secret);
  await page.getByRole("button", { name: "Test connection", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Connected as Phone agent. No role was created.");
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toContain(secret);
  await page.getByRole("button", { name: "I’ve saved my token" }).click();
  await expect(page.getByLabel("New token", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("agent-access-mobile.png"), fullPage: true });
  await page.reload();
  await page.getByRole("button", { name: "Agent access", exact: true }).click();
  await expect(page.locator(".agent-token")).toContainText("Phone agent");
  await expect(page.getByLabel("New token", { exact: true })).toHaveCount(0);
  await page.getByLabel("Token to test", { exact: true }).fill(secret);
  await page.getByRole("button", { name: "Test saved token", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Connected as Phone agent");
  await expect(page.getByLabel("Token to test", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "Revoke Phone agent", exact: true }).click();
  await page.getByRole("button", { name: "Confirm revoke", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Revoked Phone agent");
  await expect(page.locator(".agent-token-status")).toHaveText("revoked");
  await page.getByLabel("Token to test", { exact: true }).fill(secret);
  await page.getByRole("button", { name: "Test saved token", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Authentication required");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Agent access." })).toBeVisible();
  expect((await (await page.request.get(`${api}/jobs`)).json()).jobs).toHaveLength(0);
});

test("failed creation and expired sessions do not create or lose track of tokens", async ({ page }) => {
  await openAccess(page);
  const initialCount = (await (await page.request.get(`${api}/agent-tokens`)).json()).tokens.length;
  await fillCreate(page, "Recovery agent");
  await page.getByLabel("Current workspace password", { exact: true }).fill("incorrect-password");
  await page.getByRole("button", { name: "Create token", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Current password is incorrect");
  await expect(page.getByLabel("Token name", { exact: true })).toHaveValue("Recovery agent");
  await expect(page.getByLabel("Current workspace password", { exact: true })).toHaveValue("");
  expect((await (await page.request.get(`${api}/agent-tokens`)).json()).tokens).toHaveLength(initialCount);
  const session = await (await page.request.get(`${api}/auth/session`)).json();
  await page.request.post(`${api}/auth/logout`, { headers: { "X-CSRF-Token": session.csrf_token } });
  await page.getByLabel("Current workspace password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Create token", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign back in", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("Token name", { exact: true })).toHaveValue("Recovery agent");
  await expect(page.getByLabel("New token", { exact: true })).toHaveCount(0);
  expect((await (await page.request.get(`${api}/agent-tokens`)).json()).tokens).toHaveLength(initialCount);
  await page.getByLabel("Current workspace password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Create token", exact: true }).click();
  await expect(page.getByLabel("New token", { exact: true })).toBeVisible();
  expect((await (await page.request.get(`${api}/agent-tokens`)).json()).tokens).toHaveLength(initialCount + 1);
});

test("review-stage roles appear before Interesting and can be promoted", async ({ page }) => {
  await openAccess(page);
  const session = await (await page.request.get(`${api}/auth/session`)).json();
  const headers = { "X-CSRF-Token": session.csrf_token };
  const manual = await (await page.request.post(`${api}/jobs`, { headers, data: { title: "Manual opportunity" } })).json();
  expect(manual.stage).toBe("interesting");
  await page.request.post(`${api}/jobs`, { headers, data: { title: "Agent opportunity", stage: "needs_review" } });
  await page.reload();
  await page.getByRole("button", { name: "Board", exact: true }).click();
  const columns = page.locator(".board-column");
  await expect(columns.first().getByRole("heading")).toContainText("Needs review");
  await expect(columns.first()).toContainText("Agent opportunity");
  await expect(columns.nth(1)).toContainText("Manual opportunity");
  await page.getByRole("button", { name: /Agent opportunity/ }).click();
  await expect(page.getByLabel("Stage", { exact: true })).toHaveValue("needs_review");
  await page.getByLabel("Stage", { exact: true }).selectOption("interesting");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  await expect(page.getByLabel("Stage", { exact: true })).toHaveValue("interesting");
});

test("connection testing refuses redirects before forwarding the bearer token", async ({ page }) => {
  await openAccess(page);
  let forwarded = false;
  await page.route(`**${api}/redirect-target`, route => {
    forwarded = true;
    return route.fulfill({ json: { connected: true, agent_token: { name: "Redirected" } } });
  });
  await page.route(`**${api}/agent-connection`, route => route.fulfill({
    status: 307, headers: { Location: `${api}/redirect-target` }, body: "",
  }));
  await page.getByLabel("Token to test", { exact: true }).fill("disposable-test-token-" + "x".repeat(32));
  await page.getByRole("button", { name: "Test saved token", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("Token to test", { exact: true })).toHaveValue("");
  expect(forwarded).toBe(false);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
